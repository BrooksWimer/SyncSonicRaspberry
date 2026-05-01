// pw_delay_filter: Slice 2 stereo elastic engine.
//
// One process per speaker. Four PipeWire DSP ports (input_FL, input_FR,
// output_FL, output_FR). Two ring buffers (one per channel) sharing a
// single write index so left/right stay sample-locked. Read position
// is fractional (subsample) and slews smoothly toward a moving target,
// so changing the delay no longer requires killing and respawning the
// process and no longer causes a graph xrun.
//
// Control surface
// ---------------
// A small POSIX-thread companion thread binds a Unix-domain socket at
// the path passed as argv[3] (or, by default, /tmp/syncsonic-engine/
// <node_name>.sock) and accepts simple line-based commands:
//
//   set_delay <ms>              target delay in milliseconds
//   set_rate_ppm <int>          per-frame read-rate offset, clamped to ±50
//   set_mute_ramp_ms <int>      ramp-mute starting now over <ms>
//   query                       returns one JSON line on the socket
//   quit                        clean shutdown
//
// The audio thread reads only atomics; the control thread writes only
// atomics. No locks taken on the audio path.
//
// CLI
// ---
//   pw_delay_filter <delay_ms> <node_name> [<socket_path>]
//
// argv[1]  initial target delay in ms (slewed in from 0 at startup)
// argv[2]  PipeWire node name; ports become <name>:input_FL, _FR,
//          output_FL, output_FR (so pw-link from the Python side can
//          target stable port names)
// argv[3]  optional control socket path; defaults to
//          /tmp/syncsonic-engine/<node_name>.sock
//
// Build
// -----
//   gcc -O2 -Wall -Wextra -pthread -o tools/pw_delay_filter \
//       tools/pw_delay_filter.c $(pkg-config --cflags --libs libpipewire-0.3)
//
// (start_syncsonic.sh's auto-rebuild block already handles the rebuild
// when this source is newer than the binary; the Makefile target keeps
// gcc as-is. The -pthread flag will be added in start_syncsonic.sh in
// a follow-up commit when we deploy.)

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#include <pipewire/filter.h>
#include <pipewire/keys.h>
#include <pipewire/pipewire.h>

// -----------------------------------------------------------------------
// Configuration
// -----------------------------------------------------------------------
#define SAMPLE_RATE 48000
#define HEADROOM_SAMPLES 8192
// Sized for hybrid BT+Wi-Fi configurations: a Sonos chain through
// FFmpeg+Icecast+UPnP routinely shows 3-4 s of total acoustic lag on
// consumer Sonos hardware (measured 3.65 s on a Living Room ZPS9 in
// April 2026). BT speakers must be pulled UP that far to align with
// the Wi-Fi anchor, so the per-speaker ring buffer needs to be sized
// for the worst Sonos in the room, not the worst BT speaker.
//
// Each filter ring is ``MAX_DELAY_MS / 1000 * 48 kHz * 4 B`` floats
// per channel, doubled for stereo: at 5000 ms that's ~1.92 MB per
// speaker, negligible against the Pi 4's RAM but it does mean the
// filter binary must be rebuilt whenever this constant changes.
#define MAX_DELAY_MS 5000.0
#define MAX_RATE_PPM 50           // hard cap; the architecture proposal Section 4.3 rationale
#define SLEW_SAMPLES_PER_FRAME 4  // ~83 ppm during slew, inaudible on music
#define DEFAULT_SOCKET_DIR "/tmp/syncsonic-engine"
#define CMD_LINE_MAX 256

// -----------------------------------------------------------------------
// Shared state - audio thread reads, control thread writes
// -----------------------------------------------------------------------
struct shared_state {
    atomic_uint target_delay_samples;     // where current_delay_samples slews toward
    atomic_int  rate_ppm;                 // signed; clamped to ±MAX_RATE_PPM
    atomic_int  shutdown_requested;       // control thread asks audio thread to quit

    // Slice 3.2 gain control (soft-mute / unmute / partial dim).
    // target_gain_x1000 is the destination on a 0..1000 = 0.0..1.0 scale.
    // gain_ramp_samples is the wall-clock distance (in audio samples)
    // a FULL 0->1 transition would take. Partial transitions take
    // proportionally less time (slew rate is 1.0/gain_ramp_samples
    // per sample). gain_ramp_samples=0 means instant snap (clicks!).
    atomic_int  target_gain_x1000;
    atomic_int  gain_ramp_samples;

    // Stats - audio thread writes, control thread reads
    atomic_ullong frames_in_total;
    atomic_ullong frames_out_total;
    atomic_uint   queue_depth_samples;    // current_delay_samples integer part
    atomic_uint   current_delay_samples_x100;  // *100 for centi-sample precision
    atomic_int    current_gain_x1000;     // for query introspection
};

// -----------------------------------------------------------------------
// Audio-thread-only state
// -----------------------------------------------------------------------
struct audio_state {
    float   *ring_fl;
    float   *ring_fr;
    uint32_t ring_capacity;
    uint32_t write_index;             // shared between channels
    float    current_delay_samples;   // float for fractional interpolation
    double   rate_phase_acc;          // accumulator for ±ppm rate
    float    current_gain;            // 0.0..1.0; slews toward target each sample
};

// -----------------------------------------------------------------------
// App state
// -----------------------------------------------------------------------
struct app_data {
    struct pw_main_loop *loop;
    struct pw_filter    *filter;

    void *in_port_fl;
    void *in_port_fr;
    void *out_port_fl;
    void *out_port_fr;

    struct shared_state shared;
    struct audio_state  audio;

    // Control thread
    pthread_t   control_thread;
    int         control_thread_running;
    int         server_fd;
    char        socket_path[256];
};

// -----------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------
static uint32_t clamp_delay_samples(double delay_ms) {
    if (delay_ms < 0.0) delay_ms = 0.0;
    if (delay_ms > MAX_DELAY_MS) delay_ms = MAX_DELAY_MS;
    return (uint32_t) (((delay_ms / 1000.0) * (double)SAMPLE_RATE) + 0.5);
}

static int clamp_rate_ppm(int v) {
    if (v >  MAX_RATE_PPM) return  MAX_RATE_PPM;
    if (v < -MAX_RATE_PPM) return -MAX_RATE_PPM;
    return v;
}

static void make_default_socket_path(char *out, size_t n, const char *node_name) {
    // Best-effort mkdir; ignore failure (open(2) below will surface real errors).
    mkdir(DEFAULT_SOCKET_DIR, 0700);
    snprintf(out, n, "%s/%s.sock", DEFAULT_SOCKET_DIR, node_name);
}

// -----------------------------------------------------------------------
// PipeWire callbacks
// -----------------------------------------------------------------------
static void on_state_changed(
    void *userdata,
    enum pw_filter_state old_state,
    enum pw_filter_state new_state,
    const char *error
) {
    struct app_data *data = userdata;
    (void)old_state;
    if (new_state == PW_FILTER_STATE_ERROR) {
        fprintf(stderr, "pw_delay_filter: state error: %s\n", error ? error : "unknown");
        atomic_store(&data->shared.shutdown_requested, 1);
        pw_main_loop_quit(data->loop);
    }
}

static inline float ring_read_lerp(const float *ring, uint32_t cap, float pos) {
    // pos may be negative or > cap; normalise into [0, cap)
    while (pos < 0.0f)         pos += (float)cap;
    while (pos >= (float)cap)  pos -= (float)cap;
    uint32_t i0 = (uint32_t)pos;
    uint32_t i1 = (i0 + 1) % cap;
    float    frac = pos - (float)i0;
    return ring[i0] * (1.0f - frac) + ring[i1] * frac;
}

static void on_process(void *userdata, struct spa_io_position *position) {
    struct app_data *data = userdata;
    uint32_t n_samples = 1024;
    if (position != NULL && position->clock.duration > 0) {
        n_samples = position->clock.duration;
    }

    float *in_fl  = pw_filter_get_dsp_buffer(data->in_port_fl,  n_samples);
    float *in_fr  = pw_filter_get_dsp_buffer(data->in_port_fr,  n_samples);
    float *out_fl = pw_filter_get_dsp_buffer(data->out_port_fl, n_samples);
    float *out_fr = pw_filter_get_dsp_buffer(data->out_port_fr, n_samples);

    if (out_fl == NULL || out_fr == NULL) return;

    if (data->audio.ring_fl == NULL || data->audio.ring_fr == NULL ||
        data->audio.ring_capacity == 0) {
        memset(out_fl, 0, n_samples * sizeof(float));
        memset(out_fr, 0, n_samples * sizeof(float));
        return;
    }

    uint32_t target = atomic_load_explicit(&data->shared.target_delay_samples, memory_order_relaxed);
    int      rate   = atomic_load_explicit(&data->shared.rate_ppm,             memory_order_relaxed);
    if (target > data->audio.ring_capacity - 1) target = data->audio.ring_capacity - 1;

    // Slice 3.2 soft-mute: read target gain (0..1) and ramp sample
    // distance. We compute a per-sample gain step inside the loop so
    // a target change mid-callback takes effect on the very next
    // sample, not on the next callback boundary.
    int target_gain_int = atomic_load_explicit(&data->shared.target_gain_x1000, memory_order_relaxed);
    if (target_gain_int < 0)    target_gain_int = 0;
    if (target_gain_int > 1000) target_gain_int = 1000;
    const float target_gain = (float)target_gain_int * 0.001f;
    int ramp_samples = atomic_load_explicit(&data->shared.gain_ramp_samples, memory_order_relaxed);
    if (ramp_samples < 0) ramp_samples = 0;
    // step magnitude per sample for a full 0->1 transition over
    // ramp_samples; partial transitions take proportionally less
    // time. ramp_samples=0 -> instant snap (Coordinator should never
    // send 0 because it clicks).
    const float gain_step_full = (ramp_samples > 0) ? (1.0f / (float)ramp_samples) : 1.0f;

    const double ppm_per_frame = (double)rate * 1.0e-6;
    const float  cur_target    = (float)target;
    float        cur_delay     = data->audio.current_delay_samples;
    double       phase_acc     = data->audio.rate_phase_acc;
    uint32_t     w             = data->audio.write_index;
    const uint32_t cap         = data->audio.ring_capacity;
    float        gain          = data->audio.current_gain;

    for (uint32_t i = 0; i < n_samples; i++) {
        // 1. Write input into both rings at the shared write index.
        float s_fl = in_fl ? in_fl[i] : 0.0f;
        float s_fr = in_fr ? in_fr[i] : 0.0f;
        data->audio.ring_fl[w] = s_fl;
        data->audio.ring_fr[w] = s_fr;

        // 2. Slew current_delay toward target at SLEW_SAMPLES_PER_FRAME max.
        float diff = cur_target - cur_delay;
        if (diff >  SLEW_SAMPLES_PER_FRAME) diff =  SLEW_SAMPLES_PER_FRAME;
        if (diff < -SLEW_SAMPLES_PER_FRAME) diff = -SLEW_SAMPLES_PER_FRAME;
        cur_delay += diff;

        // 3. Read at fractional position w - cur_delay + rate_phase_acc.
        phase_acc += ppm_per_frame;
        float read_pos = (float)w - cur_delay - (float)phase_acc;
        float y_fl = ring_read_lerp(data->audio.ring_fl, cap, read_pos);
        float y_fr = ring_read_lerp(data->audio.ring_fr, cap, read_pos);

        // 4. Slew current gain toward target_gain. Step size is fixed
        //    by gain_step_full; we clamp so we never overshoot. A
        //    target change of 1.0 (full mute or full unmute) takes
        //    exactly ramp_samples samples; smaller changes finish
        //    proportionally sooner.
        float gain_diff = target_gain - gain;
        if (gain_diff > 0.0f) {
            float step = gain_step_full;
            if (step > gain_diff) step = gain_diff;
            gain += step;
        } else if (gain_diff < 0.0f) {
            float step = -gain_step_full;
            if (step < gain_diff) step = gain_diff;
            gain += step;
        }

        out_fl[i] = y_fl * gain;
        out_fr[i] = y_fr * gain;

        w = (w + 1) % cap;
    }

    data->audio.current_delay_samples = cur_delay;
    data->audio.rate_phase_acc        = phase_acc;
    data->audio.write_index           = w;
    data->audio.current_gain          = gain;

    // Publish stats.
    atomic_fetch_add_explicit(&data->shared.frames_in_total,  n_samples, memory_order_relaxed);
    atomic_fetch_add_explicit(&data->shared.frames_out_total, n_samples, memory_order_relaxed);
    atomic_store_explicit(&data->shared.queue_depth_samples, (uint32_t)cur_delay, memory_order_relaxed);
    atomic_store_explicit(&data->shared.current_delay_samples_x100,
                          (uint32_t)(cur_delay * 100.0f), memory_order_relaxed);
    atomic_store_explicit(&data->shared.current_gain_x1000,
                          (int)(gain * 1000.0f + 0.5f), memory_order_relaxed);
}

static const struct pw_filter_events filter_events = {
    PW_VERSION_FILTER_EVENTS,
    .state_changed = on_state_changed,
    .process       = on_process,
};

// -----------------------------------------------------------------------
// Control thread (Unix socket)
// -----------------------------------------------------------------------
static int read_line(int fd, char *buf, size_t n) {
    size_t off = 0;
    while (off + 1 < n) {
        ssize_t r = read(fd, buf + off, 1);
        if (r <= 0) return (int)r;
        if (buf[off] == '\n') break;
        off++;
    }
    buf[off] = '\0';
    // strip trailing \r if present
    if (off > 0 && buf[off - 1] == '\r') buf[off - 1] = '\0';
    return (int)off;
}

static void handle_query(struct app_data *data, int client_fd) {
    char json[640];
    int n = snprintf(
        json, sizeof(json),
        "{\"ok\":true,"
        "\"target_delay_samples\":%u,"
        "\"current_delay_samples_x100\":%u,"
        "\"rate_ppm\":%d,"
        "\"queue_depth_samples\":%u,"
        "\"frames_in_total\":%llu,"
        "\"frames_out_total\":%llu,"
        "\"target_gain_x1000\":%d,"
        "\"current_gain_x1000\":%d,"
        "\"gain_ramp_samples\":%d,"
        "\"ring_capacity\":%u}\n",
        atomic_load(&data->shared.target_delay_samples),
        atomic_load(&data->shared.current_delay_samples_x100),
        atomic_load(&data->shared.rate_ppm),
        atomic_load(&data->shared.queue_depth_samples),
        (unsigned long long)atomic_load(&data->shared.frames_in_total),
        (unsigned long long)atomic_load(&data->shared.frames_out_total),
        atomic_load(&data->shared.target_gain_x1000),
        atomic_load(&data->shared.current_gain_x1000),
        atomic_load(&data->shared.gain_ramp_samples),
        data->audio.ring_capacity
    );
    if (n > 0) (void)write(client_fd, json, (size_t)n);
}

static void handle_command(struct app_data *data, int client_fd, char *line) {
    char *cmd = strtok(line, " \t");
    if (cmd == NULL) {
        (void)write(client_fd, "{\"ok\":false,\"err\":\"empty\"}\n", 26);
        return;
    }
    if (strcmp(cmd, "set_delay") == 0) {
        char *arg = strtok(NULL, " \t");
        if (!arg) { (void)write(client_fd, "{\"ok\":false,\"err\":\"missing_ms\"}\n", 32); return; }
        double ms = strtod(arg, NULL);
        uint32_t s = clamp_delay_samples(ms);
        if (s >= data->audio.ring_capacity) s = data->audio.ring_capacity - 1;
        atomic_store(&data->shared.target_delay_samples, s);
        char ack[64];
        int n = snprintf(ack, sizeof(ack),
                         "{\"ok\":true,\"target_delay_samples\":%u}\n", s);
        (void)write(client_fd, ack, (size_t)n);
    } else if (strcmp(cmd, "set_rate_ppm") == 0) {
        char *arg = strtok(NULL, " \t");
        if (!arg) { (void)write(client_fd, "{\"ok\":false,\"err\":\"missing_ppm\"}\n", 33); return; }
        int v = clamp_rate_ppm(atoi(arg));
        atomic_store(&data->shared.rate_ppm, v);
        char ack[64];
        int n = snprintf(ack, sizeof(ack), "{\"ok\":true,\"rate_ppm\":%d}\n", v);
        (void)write(client_fd, ack, (size_t)n);
    } else if (strcmp(cmd, "mute_to") == 0) {
        // Slice 3.2: mute_to <gain_x1000> <ramp_ms>
        // Set target gain (0..1000 = silent..full) over <ramp_ms>.
        // The audio thread slews current_gain toward target one
        // sample at a time at rate 1.0/ramp_samples per sample.
        char *arg_g = strtok(NULL, " \t");
        char *arg_r = strtok(NULL, " \t");
        if (!arg_g || !arg_r) {
            (void)write(client_fd, "{\"ok\":false,\"err\":\"usage:mute_to <gain_x1000> <ramp_ms>\"}\n", 59);
            return;
        }
        int gain = atoi(arg_g);
        int ms   = atoi(arg_r);
        if (gain < 0)    gain = 0;
        if (gain > 1000) gain = 1000;
        if (ms < 0)      ms   = 0;
        if (ms > 5000)   ms   = 5000;
        int ramp_samples = (ms * SAMPLE_RATE) / 1000;
        atomic_store(&data->shared.target_gain_x1000,  gain);
        atomic_store(&data->shared.gain_ramp_samples,  ramp_samples);
        char ack[96];
        int n = snprintf(ack, sizeof(ack),
                         "{\"ok\":true,\"target_gain_x1000\":%d,\"ramp_samples\":%d}\n",
                         gain, ramp_samples);
        (void)write(client_fd, ack, (size_t)n);
    } else if (strcmp(cmd, "query") == 0) {
        handle_query(data, client_fd);
    } else if (strcmp(cmd, "quit") == 0) {
        (void)write(client_fd, "{\"ok\":true,\"bye\":true}\n", 23);
        atomic_store(&data->shared.shutdown_requested, 1);
        pw_main_loop_quit(data->loop);
    } else {
        char err[128];
        int n = snprintf(err, sizeof(err), "{\"ok\":false,\"err\":\"unknown_cmd\",\"cmd\":\"%s\"}\n", cmd);
        (void)write(client_fd, err, (size_t)n);
    }
}

static void *control_thread_main(void *arg) {
    struct app_data *data = arg;

    // Allow accept() to be unblocked when shutdown_requested goes true:
    // we use a 200ms recv timeout via SO_RCVTIMEO on the *server* fd
    // by polling around accept() with a short alarm. Simpler: just use
    // a short setsockopt timeout on accept.
    struct timeval tv = { .tv_sec = 0, .tv_usec = 200 * 1000 };
    setsockopt(data->server_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    while (!atomic_load(&data->shared.shutdown_requested)) {
        struct sockaddr_un cli;
        socklen_t cli_len = sizeof(cli);
        int client_fd = accept(data->server_fd, (struct sockaddr *)&cli, &cli_len);
        if (client_fd < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue;
            break;
        }
        // Per-client read loop: keep handling commands until the client
        // closes the connection or sends "quit".
        char line[CMD_LINE_MAX];
        while (!atomic_load(&data->shared.shutdown_requested)) {
            int n = read_line(client_fd, line, sizeof(line));
            if (n <= 0) break;
            handle_command(data, client_fd, line);
        }
        close(client_fd);
    }
    return NULL;
}

static int start_control_thread(struct app_data *data) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        fprintf(stderr, "pw_delay_filter: socket() failed: %s\n", strerror(errno));
        return -1;
    }
    // Best-effort unlink of any stale path - if the previous instance
    // crashed without cleanup, the bind() below would otherwise
    // EADDRINUSE.
    unlink(data->socket_path);

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, data->socket_path, sizeof(addr.sun_path) - 1);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "pw_delay_filter: bind(%s) failed: %s\n",
                data->socket_path, strerror(errno));
        close(fd);
        return -1;
    }
    if (listen(fd, 4) < 0) {
        fprintf(stderr, "pw_delay_filter: listen() failed: %s\n", strerror(errno));
        close(fd);
        unlink(data->socket_path);
        return -1;
    }
    chmod(data->socket_path, 0600);
    data->server_fd = fd;
    if (pthread_create(&data->control_thread, NULL, control_thread_main, data) != 0) {
        fprintf(stderr, "pw_delay_filter: pthread_create failed: %s\n", strerror(errno));
        close(fd);
        unlink(data->socket_path);
        data->server_fd = -1;
        return -1;
    }
    data->control_thread_running = 1;
    return 0;
}

static void stop_control_thread(struct app_data *data) {
    atomic_store(&data->shared.shutdown_requested, 1);
    if (data->server_fd >= 0) {
        shutdown(data->server_fd, SHUT_RDWR);
        close(data->server_fd);
        data->server_fd = -1;
    }
    if (data->control_thread_running) {
        pthread_join(data->control_thread, NULL);
        data->control_thread_running = 0;
    }
    if (data->socket_path[0]) unlink(data->socket_path);
}

// -----------------------------------------------------------------------
// Main
// -----------------------------------------------------------------------
int main(int argc, char *argv[]) {
    struct app_data data;
    double delay_ms = 0.0;
    const char *node_name = "syncsonic-delay-filter";
    const char *socket_arg = NULL;

    memset(&data, 0, sizeof(data));
    data.server_fd = -1;

    if (argc > 1) delay_ms = strtod(argv[1], NULL);
    if (argc > 2 && argv[2] && argv[2][0]) node_name = argv[2];
    if (argc > 3 && argv[3] && argv[3][0]) socket_arg = argv[3];

    if (socket_arg) {
        strncpy(data.socket_path, socket_arg, sizeof(data.socket_path) - 1);
    } else {
        make_default_socket_path(data.socket_path, sizeof(data.socket_path), node_name);
    }

    // Init shared state.
    uint32_t initial_target = clamp_delay_samples(delay_ms);
    atomic_store(&data.shared.target_delay_samples, initial_target);
    atomic_store(&data.shared.rate_ppm, 0);
    atomic_store(&data.shared.shutdown_requested, 0);
    // Slice 3.2: gain starts at full volume with no ramp pending so
    // a freshly-started filter is not silent.
    atomic_store(&data.shared.target_gain_x1000, 1000);
    atomic_store(&data.shared.gain_ramp_samples, 0);
    atomic_store(&data.shared.current_gain_x1000, 1000);
    atomic_store(&data.shared.frames_in_total, 0);
    atomic_store(&data.shared.frames_out_total, 0);
    atomic_store(&data.shared.queue_depth_samples, 0);
    atomic_store(&data.shared.current_delay_samples_x100, 0);

    // Allocate ring buffers. Capacity = MAX_DELAY * SAMPLE_RATE +
    // headroom. We always allocate the same size regardless of the
    // initial delay so the cap never has to grow at runtime when the
    // operator slides the delay up.
    data.audio.ring_capacity = (uint32_t)((MAX_DELAY_MS / 1000.0) * SAMPLE_RATE) + HEADROOM_SAMPLES;
    data.audio.ring_fl = calloc(data.audio.ring_capacity, sizeof(float));
    data.audio.ring_fr = calloc(data.audio.ring_capacity, sizeof(float));
    if (!data.audio.ring_fl || !data.audio.ring_fr) {
        fprintf(stderr, "pw_delay_filter: ring buffer allocation failed\n");
        free(data.audio.ring_fl); free(data.audio.ring_fr);
        return 1;
    }
    data.audio.write_index = 0;
    data.audio.current_delay_samples = (float)initial_target;
    data.audio.rate_phase_acc = 0.0;
    data.audio.current_gain = 1.0f;

    // Ignore SIGPIPE so a client closing the socket mid-write doesn't
    // kill the whole process.
    signal(SIGPIPE, SIG_IGN);

    pw_init(&argc, &argv);
    data.loop = pw_main_loop_new(NULL);
    if (!data.loop) {
        fprintf(stderr, "pw_delay_filter: pw_main_loop_new failed\n");
        free(data.audio.ring_fl); free(data.audio.ring_fr);
        pw_deinit();
        return 1;
    }

    struct pw_properties *props = pw_properties_new(
        PW_KEY_NODE_NAME,      node_name,
        PW_KEY_MEDIA_TYPE,     "Audio",
        PW_KEY_MEDIA_CATEGORY, "Filter",
        PW_KEY_MEDIA_ROLE,     "DSP",
        NULL
    );
    data.filter = pw_filter_new_simple(
        pw_main_loop_get_loop(data.loop), node_name, props, &filter_events, &data
    );
    if (!data.filter) {
        fprintf(stderr, "pw_delay_filter: pw_filter_new_simple failed\n");
        pw_main_loop_destroy(data.loop);
        free(data.audio.ring_fl); free(data.audio.ring_fr);
        pw_deinit();
        return 1;
    }

    // Four DSP ports: input_FL, input_FR, output_FL, output_FR.
    // The Python side links by these exact names so they are part of
    // the contract; do not rename without also updating
    // pipewire_transport.py.
    struct {
        const char *name;
        enum pw_direction dir;
        void **out;
    } ports[] = {
        { "input_FL",  PW_DIRECTION_INPUT,  &data.in_port_fl  },
        { "input_FR",  PW_DIRECTION_INPUT,  &data.in_port_fr  },
        { "output_FL", PW_DIRECTION_OUTPUT, &data.out_port_fl },
        { "output_FR", PW_DIRECTION_OUTPUT, &data.out_port_fr },
    };
    for (size_t i = 0; i < sizeof(ports) / sizeof(ports[0]); i++) {
        *ports[i].out = pw_filter_add_port(
            data.filter,
            ports[i].dir,
            PW_FILTER_PORT_FLAG_MAP_BUFFERS,
            0,
            pw_properties_new(
                PW_KEY_FORMAT_DSP, "32 bit float mono audio",
                PW_KEY_PORT_NAME,  ports[i].name,
                NULL
            ),
            NULL,
            0
        );
        if (*ports[i].out == NULL) {
            fprintf(stderr, "pw_delay_filter: port %s creation failed\n", ports[i].name);
            pw_filter_destroy(data.filter);
            pw_main_loop_destroy(data.loop);
            free(data.audio.ring_fl); free(data.audio.ring_fr);
            pw_deinit();
            return 1;
        }
    }

    int rc = pw_filter_connect(data.filter, PW_FILTER_FLAG_RT_PROCESS, NULL, 0);
    if (rc < 0) {
        fprintf(stderr, "pw_delay_filter: pw_filter_connect failed: %d\n", rc);
        pw_filter_destroy(data.filter);
        pw_main_loop_destroy(data.loop);
        free(data.audio.ring_fl); free(data.audio.ring_fr);
        pw_deinit();
        return 1;
    }

    if (start_control_thread(&data) != 0) {
        fprintf(stderr, "pw_delay_filter: control thread failed; running without IPC\n");
    }

    fprintf(stderr,
            "pw_delay_filter: stereo elastic engine running\n"
            "  node=%s\n"
            "  initial_delay=%.1f ms (%u samples)\n"
            "  ring_capacity=%u samples (~%.0f ms)\n"
            "  socket=%s\n",
            node_name, delay_ms, initial_target,
            data.audio.ring_capacity,
            (double)data.audio.ring_capacity * 1000.0 / (double)SAMPLE_RATE,
            data.socket_path);

    pw_main_loop_run(data.loop);

    stop_control_thread(&data);
    pw_filter_destroy(data.filter);
    pw_main_loop_destroy(data.loop);
    free(data.audio.ring_fl); free(data.audio.ring_fr);
    pw_deinit();
    return 0;
}
