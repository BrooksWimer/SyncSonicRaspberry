#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <time.h>

#include <pipewire/filter.h>
#include <pipewire/keys.h>
#include <pipewire/pipewire.h>

struct app_data {
    struct pw_main_loop *loop;
    struct pw_filter *filter;
    void *in_port;
    void *out_port;
    float *delay_buffer;
    uint32_t delay_capacity;
    uint32_t delay_samples;
    uint32_t write_index;
    char node_name[128];
    uint64_t cycles_total;
    uint64_t samples_total;
    uint64_t late_cycles_total;
    uint64_t silence_cycles_total;
    uint64_t last_cb_ns;
    uint64_t last_log_ns;
    uint64_t telemetry_interval_ns;
    float last_gap_ms;
    float last_expected_ms;
    float last_avg_abs;
    float last_peak_abs;
};

static uint64_t monotonic_time_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0;
    }
    return (((uint64_t) ts.tv_sec) * 1000000000ULL) + (uint64_t) ts.tv_nsec;
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
        fprintf(stderr, "pw_delay_filter: state error: %s\n", error ? error : "unknown");
        pw_main_loop_quit(data->loop);
    }
}

static void on_process(void *userdata, struct spa_io_position *position) {
    struct app_data *data = userdata;
    uint32_t n_samples = 1024;
    uint32_t i;
    float *in;
    float *out;
    float avg_abs = 0.0f;
    float peak_abs = 0.0f;
    uint64_t now_ns;
    float expected_ms;
    float gap_ms = 0.0f;
    int late_cycle = 0;
    int silence_cycle = 0;
    const float silence_threshold = 0.0015f;

    if (position != NULL && position->clock.duration > 0) {
        n_samples = position->clock.duration;
    }

    in = pw_filter_get_dsp_buffer(data->in_port, n_samples);
    out = pw_filter_get_dsp_buffer(data->out_port, n_samples);
    if (out == NULL) {
        return;
    }

    if (data->delay_buffer == NULL || data->delay_capacity == 0) {
        memset(out, 0, n_samples * sizeof(float));
        return;
    }

    for (i = 0; i < n_samples; i++) {
        float sample = in ? in[i] : 0.0f;
        float abs_sample;
        uint32_t read_index;

        data->delay_buffer[data->write_index] = sample;
        read_index = (data->write_index + data->delay_capacity - data->delay_samples) % data->delay_capacity;
        out[i] = data->delay_buffer[read_index];
        data->write_index = (data->write_index + 1) % data->delay_capacity;

        abs_sample = sample >= 0.0f ? sample : -sample;
        avg_abs += abs_sample;
        if (abs_sample > peak_abs) {
            peak_abs = abs_sample;
        }
    }

    avg_abs = n_samples > 0 ? (avg_abs / (float) n_samples) : 0.0f;
    silence_cycle = avg_abs < silence_threshold ? 1 : 0;

    now_ns = (position != NULL && position->clock.nsec > 0)
        ? position->clock.nsec
        : monotonic_time_ns();
    expected_ms = ((float) n_samples * 1000.0f) / 48000.0f;
    if (data->last_cb_ns > 0 && now_ns > data->last_cb_ns) {
        gap_ms = (float) (now_ns - data->last_cb_ns) / 1000000.0f;
    }
    if (gap_ms > (expected_ms * 1.75f) && expected_ms > 0.0f) {
        late_cycle = 1;
    }

    data->cycles_total += 1;
    data->samples_total += n_samples;
    if (late_cycle) {
        data->late_cycles_total += 1;
    }
    if (silence_cycle) {
        data->silence_cycles_total += 1;
    }
    data->last_cb_ns = now_ns;
    data->last_gap_ms = gap_ms;
    data->last_expected_ms = expected_ms;
    data->last_avg_abs = avg_abs;
    data->last_peak_abs = peak_abs;

    if (late_cycle) {
        fprintf(
            stderr,
            "PW_DSP_EVT node=%s type=callback_gap gap_ms=%.3f expected_ms=%.3f cycles=%" PRIu64 "\n",
            data->node_name,
            gap_ms,
            expected_ms,
            data->cycles_total
        );
    }

    if (data->telemetry_interval_ns > 0 && now_ns > 0 && (now_ns - data->last_log_ns) >= data->telemetry_interval_ns) {
        float late_ratio = data->cycles_total > 0
            ? ((float) data->late_cycles_total / (float) data->cycles_total)
            : 0.0f;
        float silence_ratio = data->cycles_total > 0
            ? ((float) data->silence_cycles_total / (float) data->cycles_total)
            : 0.0f;
        fprintf(
            stderr,
            "PW_DSP node=%s cycles=%" PRIu64 " late=%" PRIu64 " late_ratio=%.3f "
            "silence_ratio=%.3f avg_abs=%.6f peak_abs=%.6f gap_ms=%.3f expected_ms=%.3f\n",
            data->node_name,
            data->cycles_total,
            data->late_cycles_total,
            late_ratio,
            silence_ratio,
            avg_abs,
            peak_abs,
            gap_ms,
            expected_ms
        );
        data->last_log_ns = now_ns;
    }
}

static const struct pw_filter_events filter_events = {
    PW_VERSION_FILTER_EVENTS,
    .state_changed = on_state_changed,
    .process = on_process,
};

static uint32_t clamp_delay_samples(double delay_ms) {
    double bounded = delay_ms;
    if (bounded < 0.0) {
        bounded = 0.0;
    }
    if (bounded > 2000.0) {
        bounded = 2000.0;
    }
    return (uint32_t) (((bounded / 1000.0) * 48000.0) + 0.5);
}

int main(int argc, char *argv[]) {
    struct app_data data;
    double delay_ms = 0.0;
    double telemetry_ms = 1000.0;
    const char *node_name = "syncsonic-delay-filter";
    struct pw_properties *props;
    const char *env_telemetry_ms;
    int rc;

    memset(&data, 0, sizeof(data));

    if (argc > 1) {
        delay_ms = strtod(argv[1], NULL);
    }
    if (argc > 2 && argv[2] != NULL && argv[2][0] != '\0') {
        node_name = argv[2];
    }
    env_telemetry_ms = getenv("SYNCSONIC_DSP_TELEMETRY_MS");
    if (env_telemetry_ms != NULL && env_telemetry_ms[0] != '\0') {
        telemetry_ms = strtod(env_telemetry_ms, NULL);
    }
    if (telemetry_ms < 0.0) {
        telemetry_ms = 0.0;
    }
    data.telemetry_interval_ns = (uint64_t) (telemetry_ms * 1000000.0);
    data.last_log_ns = monotonic_time_ns();
    snprintf(data.node_name, sizeof(data.node_name), "%s", node_name);

    data.delay_samples = clamp_delay_samples(delay_ms);
    data.delay_capacity = data.delay_samples + 8192;
    if (data.delay_capacity < 8192) {
        data.delay_capacity = 8192;
    }
    data.delay_buffer = calloc(data.delay_capacity, sizeof(float));
    if (data.delay_buffer == NULL) {
        fprintf(stderr, "pw_delay_filter: delay buffer allocation failed\n");
        return 1;
    }

    pw_init(&argc, &argv);

    data.loop = pw_main_loop_new(NULL);
    if (data.loop == NULL) {
        fprintf(stderr, "pw_delay_filter: main loop creation failed\n");
        free(data.delay_buffer);
        pw_deinit();
        return 1;
    }

    props = pw_properties_new(
        PW_KEY_NODE_NAME, node_name,
        PW_KEY_MEDIA_TYPE, "Audio",
        PW_KEY_MEDIA_CATEGORY, "Filter",
        PW_KEY_MEDIA_ROLE, "DSP",
        NULL
    );

    data.filter = pw_filter_new_simple(
        pw_main_loop_get_loop(data.loop),
        node_name,
        props,
        &filter_events,
        &data
    );
    if (data.filter == NULL) {
        fprintf(stderr, "pw_delay_filter: filter creation failed\n");
        pw_main_loop_destroy(data.loop);
        free(data.delay_buffer);
        pw_deinit();
        return 1;
    }

    data.in_port = pw_filter_add_port(
        data.filter,
        PW_DIRECTION_INPUT,
        PW_FILTER_PORT_FLAG_MAP_BUFFERS,
        0,
        pw_properties_new(
            PW_KEY_FORMAT_DSP, "32 bit float mono audio",
            PW_KEY_PORT_NAME, "input",
            NULL
        ),
        NULL,
        0
    );
    data.out_port = pw_filter_add_port(
        data.filter,
        PW_DIRECTION_OUTPUT,
        PW_FILTER_PORT_FLAG_MAP_BUFFERS,
        0,
        pw_properties_new(
            PW_KEY_FORMAT_DSP, "32 bit float mono audio",
            PW_KEY_PORT_NAME, "output",
            NULL
        ),
        NULL,
        0
    );
    if (data.in_port == NULL || data.out_port == NULL) {
        fprintf(stderr, "pw_delay_filter: port creation failed\n");
        pw_filter_destroy(data.filter);
        pw_main_loop_destroy(data.loop);
        free(data.delay_buffer);
        pw_deinit();
        return 1;
    }

    rc = pw_filter_connect(data.filter, PW_FILTER_FLAG_RT_PROCESS, NULL, 0);
    if (rc < 0) {
        fprintf(stderr, "pw_delay_filter: connect failed: %d\n", rc);
        pw_filter_destroy(data.filter);
        pw_main_loop_destroy(data.loop);
        free(data.delay_buffer);
        pw_deinit();
        return 1;
    }

    fprintf(stderr, "pw_delay_filter: running with %.1f ms delay (%u samples)\n", delay_ms, data.delay_samples);
    pw_main_loop_run(data.loop);

    pw_filter_destroy(data.filter);
    pw_main_loop_destroy(data.loop);
    free(data.delay_buffer);
    pw_deinit();
    return 0;
}
