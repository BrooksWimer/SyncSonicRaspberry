// pw_eq_filter: per-speaker stereo biquad EQ filter.
//
// One process per speaker. Four PipeWire DSP ports (input_FL, input_FR,
// output_FL, output_FR), matching pw_delay_filter's port contract so
// Python can wire the filter by stable aliases. The filter loads a JSON
// profile from backend/eq_profiles/<mac>.json and applies each enabled
// biquad band inline on the realtime audio path. No external DSP or JSON
// libraries are used.
//
// Control surface
// ---------------
// A POSIX-thread companion binds a Unix-domain socket at argv[3] (or
// /tmp/syncsonic-engine/<node_name>.sock by default) and accepts:
//
//   reload_profile               reload backend/eq_profiles/<mac>.json
//   query                        returns one JSON line
//   quit                         clean shutdown
//
// CLI
// ---
//   pw_eq_filter <mac> <node_name> [<socket_path>]

#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
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

#define SAMPLE_RATE 48000.0
#define MAX_BANDS 32
#define MAX_PROFILE_BYTES 65536
#define DEFAULT_SOCKET_DIR "/tmp/syncsonic-engine"
#define CMD_LINE_MAX 256

typedef struct {
    double b0;
    double b1;
    double b2;
    double a1;
    double a2;
} biquad_coeff_t;

typedef struct {
    double z1;
    double z2;
} biquad_state_t;

struct eq_bank {
    int band_count;
    biquad_coeff_t coeffs[MAX_BANDS];
};

struct shared_state {
    atomic_int shutdown_requested;
    atomic_uint active_bank;
    atomic_uint profile_version;
    atomic_uint profile_band_count;
    atomic_uint reload_error_count;
    atomic_ullong frames_in_total;
    atomic_ullong frames_out_total;
};

struct audio_state {
    struct eq_bank banks[2];
    biquad_state_t state_fl[MAX_BANDS];
    biquad_state_t state_fr[MAX_BANDS];
    unsigned int local_bank;
};

struct app_data {
    struct pw_main_loop *loop;
    struct pw_filter *filter;

    void *in_port_fl;
    void *in_port_fr;
    void *out_port_fl;
    void *out_port_fr;

    struct shared_state shared;
    struct audio_state audio;

    pthread_t control_thread;
    int control_thread_running;
    int server_fd;
    char socket_path[256];
    char profile_path[512];
    char mac[32];
};

static void make_default_socket_path(char *out, size_t n, const char *node_name) {
    mkdir(DEFAULT_SOCKET_DIR, 0700);
    snprintf(out, n, "%s/%s.sock", DEFAULT_SOCKET_DIR, node_name);
}

static void sanitize_mac(const char *in, char *out, size_t n) {
    size_t j = 0;
    for (size_t i = 0; in && in[i] && j + 1 < n; i++) {
        unsigned char c = (unsigned char)in[i];
        out[j++] = (char)(isalnum(c) ? toupper(c) : '_');
    }
    out[j] = '\0';
}

static void make_profile_path(char *out, size_t n, const char *argv0, const char *mac) {
    char mac_token[64];
    sanitize_mac(mac, mac_token, sizeof(mac_token));

    char resolved[512];
    if (argv0 && realpath(argv0, resolved) != NULL) {
        char *slash = strrchr(resolved, '/');
        if (slash) *slash = '\0';          // backend/tools
        slash = strrchr(resolved, '/');
        if (slash) *slash = '\0';          // backend
        snprintf(out, n, "%s/eq_profiles/%s.json", resolved, mac_token);
        return;
    }
    snprintf(out, n, "eq_profiles/%s.json", mac_token);
}

static int json_find_number(const char *obj, const char *key, double *out) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(obj, pattern);
    if (!p) return 0;
    p = strchr(p + strlen(pattern), ':');
    if (!p) return 0;
    p++;
    errno = 0;
    char *end = NULL;
    double v = strtod(p, &end);
    if (end == p || errno == ERANGE) return 0;
    *out = v;
    return 1;
}

static int json_find_bool_false(const char *obj, const char *key) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(obj, pattern);
    if (!p) return 0;
    p = strchr(p + strlen(pattern), ':');
    if (!p) return 0;
    p++;
    while (*p && isspace((unsigned char)*p)) p++;
    return strncmp(p, "false", 5) == 0;
}

static int make_peaking(double freq_hz, double gain_db, double q, biquad_coeff_t *out) {
    if (freq_hz <= 0.0 || freq_hz >= SAMPLE_RATE * 0.5 || q <= 0.0) return 0;
    double a = pow(10.0, gain_db / 40.0);
    double w0 = 2.0 * M_PI * freq_hz / SAMPLE_RATE;
    double alpha = sin(w0) / (2.0 * q);
    double cosw0 = cos(w0);

    double b0 = 1.0 + alpha * a;
    double b1 = -2.0 * cosw0;
    double b2 = 1.0 - alpha * a;
    double a0 = 1.0 + alpha / a;
    double a1 = -2.0 * cosw0;
    double a2 = 1.0 - alpha / a;
    if (fabs(a0) < 1.0e-12) return 0;

    out->b0 = b0 / a0;
    out->b1 = b1 / a0;
    out->b2 = b2 / a0;
    out->a1 = a1 / a0;
    out->a2 = a2 / a0;
    return 1;
}

static int load_profile_file(const char *path, struct eq_bank *bank) {
    FILE *fp = fopen(path, "rb");
    if (!fp) {
        bank->band_count = 0;
        return -1;
    }
    char *buf = calloc(MAX_PROFILE_BYTES + 1, 1);
    if (!buf) {
        fclose(fp);
        return -1;
    }
    size_t n = fread(buf, 1, MAX_PROFILE_BYTES, fp);
    fclose(fp);
    buf[n] = '\0';

    int count = 0;
    const char *p = buf;
    while (count < MAX_BANDS && (p = strchr(p, '{')) != NULL) {
        const char *end = strchr(p, '}');
        if (!end) break;
        size_t len = (size_t)(end - p + 1);
        if (len > 2047) len = 2047;
        char obj[2048];
        memcpy(obj, p, len);
        obj[len] = '\0';

        if (!json_find_bool_false(obj, "enabled")) {
            double b0, b1, b2, a1, a2;
            if (json_find_number(obj, "b0", &b0) &&
                json_find_number(obj, "b1", &b1) &&
                json_find_number(obj, "b2", &b2) &&
                json_find_number(obj, "a1", &a1) &&
                json_find_number(obj, "a2", &a2)) {
                bank->coeffs[count++] = (biquad_coeff_t){ b0, b1, b2, a1, a2 };
            } else {
                double freq, gain, q;
                if (json_find_number(obj, "freq_hz", &freq) &&
                    json_find_number(obj, "gain_db", &gain) &&
                    json_find_number(obj, "q", &q)) {
                    biquad_coeff_t c;
                    if (make_peaking(freq, gain, q, &c)) {
                        bank->coeffs[count++] = c;
                    }
                }
            }
        }
        p = end + 1;
    }

    bank->band_count = count;
    free(buf);
    return 0;
}

static int reload_profile(struct app_data *data) {
    unsigned int active = atomic_load_explicit(&data->shared.active_bank, memory_order_acquire);
    unsigned int next = (active + 1U) & 1U;
    struct eq_bank tmp;
    memset(&tmp, 0, sizeof(tmp));
    int rc = load_profile_file(data->profile_path, &tmp);
    data->audio.banks[next] = tmp;
    atomic_store_explicit(&data->shared.profile_band_count, (unsigned int)tmp.band_count,
                          memory_order_relaxed);
    atomic_fetch_add_explicit(&data->shared.profile_version, 1, memory_order_relaxed);
    atomic_store_explicit(&data->shared.active_bank, next, memory_order_release);
    if (rc != 0) {
        atomic_fetch_add_explicit(&data->shared.reload_error_count, 1, memory_order_relaxed);
    }
    return rc;
}

static inline float biquad_process(float x, const biquad_coeff_t *c, biquad_state_t *s) {
    double y = c->b0 * (double)x + s->z1;
    s->z1 = c->b1 * (double)x - c->a1 * y + s->z2;
    s->z2 = c->b2 * (double)x - c->a2 * y;
    if (y > 8.0) y = 8.0;
    if (y < -8.0) y = -8.0;
    return (float)y;
}

static void on_state_changed(
    void *userdata,
    enum pw_filter_state old_state,
    enum pw_filter_state new_state,
    const char *error
) {
    struct app_data *data = userdata;
    (void)old_state;
    if (new_state == PW_FILTER_STATE_ERROR) {
        fprintf(stderr, "pw_eq_filter: state error: %s\n", error ? error : "unknown");
        atomic_store(&data->shared.shutdown_requested, 1);
        pw_main_loop_quit(data->loop);
    }
}

static void on_process(void *userdata, struct spa_io_position *position) {
    struct app_data *data = userdata;
    uint32_t n_samples = 1024;
    if (position != NULL && position->clock.duration > 0) {
        n_samples = position->clock.duration;
    }

    float *in_fl = pw_filter_get_dsp_buffer(data->in_port_fl, n_samples);
    float *in_fr = pw_filter_get_dsp_buffer(data->in_port_fr, n_samples);
    float *out_fl = pw_filter_get_dsp_buffer(data->out_port_fl, n_samples);
    float *out_fr = pw_filter_get_dsp_buffer(data->out_port_fr, n_samples);
    if (out_fl == NULL || out_fr == NULL) return;

    unsigned int bank_idx = atomic_load_explicit(&data->shared.active_bank, memory_order_acquire) & 1U;
    if (bank_idx != data->audio.local_bank) {
        memset(data->audio.state_fl, 0, sizeof(data->audio.state_fl));
        memset(data->audio.state_fr, 0, sizeof(data->audio.state_fr));
        data->audio.local_bank = bank_idx;
    }
    const struct eq_bank *bank = &data->audio.banks[bank_idx];
    int band_count = bank->band_count;
    if (band_count < 0) band_count = 0;
    if (band_count > MAX_BANDS) band_count = MAX_BANDS;

    for (uint32_t i = 0; i < n_samples; i++) {
        float y_fl = in_fl ? in_fl[i] : 0.0f;
        float y_fr = in_fr ? in_fr[i] : 0.0f;
        for (int b = 0; b < band_count; b++) {
            y_fl = biquad_process(y_fl, &bank->coeffs[b], &data->audio.state_fl[b]);
            y_fr = biquad_process(y_fr, &bank->coeffs[b], &data->audio.state_fr[b]);
        }
        out_fl[i] = y_fl;
        out_fr[i] = y_fr;
    }

    atomic_fetch_add_explicit(&data->shared.frames_in_total, n_samples, memory_order_relaxed);
    atomic_fetch_add_explicit(&data->shared.frames_out_total, n_samples, memory_order_relaxed);
}

static const struct pw_filter_events filter_events = {
    PW_VERSION_FILTER_EVENTS,
    .state_changed = on_state_changed,
    .process = on_process,
};

static int read_line(int fd, char *buf, size_t n) {
    size_t off = 0;
    while (off + 1 < n) {
        ssize_t r = read(fd, buf + off, 1);
        if (r <= 0) return (int)r;
        if (buf[off] == '\n') break;
        off++;
    }
    buf[off] = '\0';
    if (off > 0 && buf[off - 1] == '\r') buf[off - 1] = '\0';
    return (int)off;
}

static void handle_query(struct app_data *data, int client_fd) {
    char json[768];
    int n = snprintf(
        json, sizeof(json),
        "{\"ok\":true,"
        "\"mac\":\"%s\","
        "\"profile_path\":\"%s\","
        "\"active_bank\":%u,"
        "\"profile_version\":%u,"
        "\"profile_band_count\":%u,"
        "\"reload_error_count\":%u,"
        "\"frames_in_total\":%llu,"
        "\"frames_out_total\":%llu}\n",
        data->mac,
        data->profile_path,
        atomic_load(&data->shared.active_bank),
        atomic_load(&data->shared.profile_version),
        atomic_load(&data->shared.profile_band_count),
        atomic_load(&data->shared.reload_error_count),
        (unsigned long long)atomic_load(&data->shared.frames_in_total),
        (unsigned long long)atomic_load(&data->shared.frames_out_total)
    );
    if (n > 0) (void)write(client_fd, json, (size_t)n);
}

static void handle_command(struct app_data *data, int client_fd, char *line) {
    char *cmd = strtok(line, " \t");
    if (cmd == NULL) {
        (void)write(client_fd, "{\"ok\":false,\"err\":\"empty\"}\n", 26);
        return;
    }
    if (strcmp(cmd, "reload_profile") == 0 || strcmp(cmd, "reload") == 0) {
        int rc = reload_profile(data);
        char ack[160];
        int n = snprintf(
            ack, sizeof(ack),
            "{\"ok\":%s,\"profile_band_count\":%u,\"profile_version\":%u}\n",
            rc == 0 ? "true" : "false",
            atomic_load(&data->shared.profile_band_count),
            atomic_load(&data->shared.profile_version)
        );
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
        fprintf(stderr, "pw_eq_filter: socket() failed: %s\n", strerror(errno));
        return -1;
    }
    unlink(data->socket_path);

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, data->socket_path, sizeof(addr.sun_path) - 1);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "pw_eq_filter: bind(%s) failed: %s\n", data->socket_path, strerror(errno));
        close(fd);
        return -1;
    }
    if (listen(fd, 4) < 0) {
        fprintf(stderr, "pw_eq_filter: listen() failed: %s\n", strerror(errno));
        close(fd);
        unlink(data->socket_path);
        return -1;
    }
    chmod(data->socket_path, 0600);
    data->server_fd = fd;
    if (pthread_create(&data->control_thread, NULL, control_thread_main, data) != 0) {
        fprintf(stderr, "pw_eq_filter: pthread_create failed: %s\n", strerror(errno));
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

int main(int argc, char *argv[]) {
    struct app_data data;
    const char *mac = "00:00:00:00:00:00";
    const char *node_name = "syncsonic-eq-filter";
    const char *socket_arg = NULL;

    memset(&data, 0, sizeof(data));
    data.server_fd = -1;

    if (argc > 1 && argv[1] && argv[1][0]) mac = argv[1];
    if (argc > 2 && argv[2] && argv[2][0]) node_name = argv[2];
    if (argc > 3 && argv[3] && argv[3][0]) socket_arg = argv[3];

    strncpy(data.mac, mac, sizeof(data.mac) - 1);
    if (socket_arg) {
        strncpy(data.socket_path, socket_arg, sizeof(data.socket_path) - 1);
    } else {
        make_default_socket_path(data.socket_path, sizeof(data.socket_path), node_name);
    }
    make_profile_path(data.profile_path, sizeof(data.profile_path), argv[0], mac);

    atomic_store(&data.shared.shutdown_requested, 0);
    atomic_store(&data.shared.active_bank, 0);
    atomic_store(&data.shared.profile_version, 0);
    atomic_store(&data.shared.profile_band_count, 0);
    atomic_store(&data.shared.reload_error_count, 0);
    atomic_store(&data.shared.frames_in_total, 0);
    atomic_store(&data.shared.frames_out_total, 0);
    data.audio.local_bank = 0;
    (void)reload_profile(&data);

    signal(SIGPIPE, SIG_IGN);

    pw_init(&argc, &argv);
    data.loop = pw_main_loop_new(NULL);
    if (!data.loop) {
        fprintf(stderr, "pw_eq_filter: pw_main_loop_new failed\n");
        pw_deinit();
        return 1;
    }

    struct pw_properties *props = pw_properties_new(
        PW_KEY_NODE_NAME, node_name,
        PW_KEY_MEDIA_TYPE, "Audio",
        PW_KEY_MEDIA_CATEGORY, "Filter",
        PW_KEY_MEDIA_ROLE, "DSP",
        NULL
    );
    data.filter = pw_filter_new_simple(
        pw_main_loop_get_loop(data.loop), node_name, props, &filter_events, &data
    );
    if (!data.filter) {
        fprintf(stderr, "pw_eq_filter: pw_filter_new_simple failed\n");
        pw_main_loop_destroy(data.loop);
        pw_deinit();
        return 1;
    }

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
                PW_KEY_PORT_NAME, ports[i].name,
                NULL
            ),
            NULL,
            0
        );
        if (*ports[i].out == NULL) {
            fprintf(stderr, "pw_eq_filter: port %s creation failed\n", ports[i].name);
            pw_filter_destroy(data.filter);
            pw_main_loop_destroy(data.loop);
            pw_deinit();
            return 1;
        }
    }

    int rc = pw_filter_connect(data.filter, PW_FILTER_FLAG_RT_PROCESS, NULL, 0);
    if (rc < 0) {
        fprintf(stderr, "pw_eq_filter: pw_filter_connect failed: %d\n", rc);
        pw_filter_destroy(data.filter);
        pw_main_loop_destroy(data.loop);
        pw_deinit();
        return 1;
    }

    if (start_control_thread(&data) != 0) {
        fprintf(stderr, "pw_eq_filter: control thread failed; running without IPC\n");
    }

    fprintf(stderr,
            "pw_eq_filter: stereo biquad EQ running\n"
            "  mac=%s\n"
            "  node=%s\n"
            "  profile=%s\n"
            "  bands=%u\n"
            "  socket=%s\n",
            data.mac,
            node_name,
            data.profile_path,
            atomic_load(&data.shared.profile_band_count),
            data.socket_path);

    pw_main_loop_run(data.loop);

    stop_control_thread(&data);
    pw_filter_destroy(data.filter);
    pw_main_loop_destroy(data.loop);
    pw_deinit();
    return 0;
}
