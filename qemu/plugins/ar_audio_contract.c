/*
 * Capture one invocation of the stock AR MKII audio kernel without reading
 * guest memory outside accesses already performed by that kernel.
 *
 * Output is NDJSON: a header, entry/exit register snapshots, ordered memory
 * accesses, and a completion footer. Firmware and PCM are not embedded.
 */
#include <glib.h>
#include <inttypes.h>
#include <qemu-plugin.h>
#include <stdio.h>
#include <string.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef struct {
    struct qemu_plugin_register *handle;
    char *name;
} Register;

static const uint64_t default_start_pc = 0x401184c4;
static const uint64_t default_end_pc = 0x401187ff;
static const uint64_t default_exit_pc = 0x40117fc2;
static uint64_t start_pc = 0x401184c4;
static uint64_t end_pc = 0x401187ff;
static uint64_t exit_pc = 0x40117fc2;
static FILE *trace_file;
static GPtrArray *registers;
static GMutex trace_lock;
static uint64_t memory_events;
static uint64_t loads;
static uint64_t stores;
static bool started;
static bool capturing;
static bool completed;

static void register_free(gpointer data)
{
    Register *reg = data;

    g_free(reg->name);
    g_free(reg);
}

static void json_string(FILE *out, const char *value)
{
    const unsigned char *p = (const unsigned char *)value;

    fputc('"', out);
    for (; *p; p++) {
        switch (*p) {
        case '"': fputs("\\\"", out); break;
        case '\\': fputs("\\\\", out); break;
        case '\b': fputs("\\b", out); break;
        case '\f': fputs("\\f", out); break;
        case '\n': fputs("\\n", out); break;
        case '\r': fputs("\\r", out); break;
        case '\t': fputs("\\t", out); break;
        default:
            if (*p < 0x20) {
                fprintf(out, "\\u%04x", *p);
            } else {
                fputc(*p, out);
            }
        }
    }
    fputc('"', out);
}

static void hex_bytes(FILE *out, const GByteArray *bytes)
{
    fputs("\"0x", out);
    for (size_t i = 0; i < bytes->len; i++) {
        fprintf(out, "%02x", bytes->data[i]);
    }
    fputc('"', out);
}

static void register_snapshot(const char *phase, uint64_t pc)
{
    g_autoptr(GByteArray) value = g_byte_array_new();

    fprintf(trace_file,
            "{\"kind\":\"boundary\",\"phase\":\"%s\","
            "\"pc\":\"0x%08" PRIx64 "\",\"registers\":{",
            phase, pc);
    for (size_t i = 0; i < registers->len; i++) {
        Register *reg = g_ptr_array_index(registers, i);

        if (i) {
            fputc(',', trace_file);
        }
        json_string(trace_file, reg->name);
        fputc(':', trace_file);
        g_byte_array_set_size(value, 0);
        if (qemu_plugin_read_register(reg->handle, value)) {
            hex_bytes(trace_file, value);
        } else {
            fputs("null", trace_file);
        }
    }
    fputs("}}\n", trace_file);
}

static void write_footer(bool complete)
{
    fprintf(trace_file,
            "{\"kind\":\"footer\",\"complete\":%s,"
            "\"memory_events\":%" PRIu64 ",\"loads\":%" PRIu64
            ",\"stores\":%" PRIu64 "}\n",
            complete ? "true" : "false", memory_events, loads, stores);
    fflush(trace_file);
}

static void boundary(unsigned int cpu_index, void *userdata)
{
    uint64_t pc = (uintptr_t)userdata;

    (void)cpu_index;
    g_mutex_lock(&trace_lock);
    if (pc == start_pc && !started) {
        started = true;
        register_snapshot("entry", pc);
        capturing = true;
    } else if (pc == exit_pc && capturing) {
        capturing = false;
        register_snapshot("exit", pc);
        completed = true;
        write_footer(true);
    }
    g_mutex_unlock(&trace_lock);
}

static void memory_access(unsigned int cpu_index, qemu_plugin_meminfo_t info,
                          uint64_t address, void *userdata)
{
    uint64_t pc = (uintptr_t)userdata;
    qemu_plugin_mem_value value;
    const char *operation;
    unsigned size;

    (void)cpu_index;
    g_mutex_lock(&trace_lock);
    if (!capturing) {
        g_mutex_unlock(&trace_lock);
        return;
    }
    value = qemu_plugin_mem_get_value(info);
    operation = qemu_plugin_mem_is_store(info) ? "store" : "load";
    size = 1u << qemu_plugin_mem_size_shift(info);
    fprintf(trace_file,
            "{\"kind\":\"memory\",\"sequence\":%" PRIu64
            ",\"pc\":\"0x%08" PRIx64 "\",\"operation\":\"%s\","
            "\"address\":\"0x%08" PRIx64 "\",\"size\":%u,"
            "\"value\":\"0x",
            memory_events, pc, operation, address, size);
    switch (value.type) {
    case QEMU_PLUGIN_MEM_VALUE_U8:
        fprintf(trace_file, "%02" PRIx8, value.data.u8);
        break;
    case QEMU_PLUGIN_MEM_VALUE_U16:
        fprintf(trace_file, "%04" PRIx16, value.data.u16);
        break;
    case QEMU_PLUGIN_MEM_VALUE_U32:
        fprintf(trace_file, "%08" PRIx32, value.data.u32);
        break;
    case QEMU_PLUGIN_MEM_VALUE_U64:
        fprintf(trace_file, "%016" PRIx64, value.data.u64);
        break;
    case QEMU_PLUGIN_MEM_VALUE_U128:
        fprintf(trace_file, "%016" PRIx64 "%016" PRIx64,
                value.data.u128.high, value.data.u128.low);
        break;
    default:
        g_assert_not_reached();
    }
    fputs("\"}\n", trace_file);
    memory_events++;
    if (qemu_plugin_mem_is_store(info)) {
        stores++;
    } else {
        loads++;
    }
    g_mutex_unlock(&trace_lock);
}

static void translate(struct qemu_plugin_tb *tb, void *userdata)
{
    size_t count = qemu_plugin_tb_n_insns(tb);

    (void)userdata;
    for (size_t i = 0; i < count; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint64_t pc = qemu_plugin_insn_vaddr(insn);

        if (pc == start_pc || pc == exit_pc) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, boundary, QEMU_PLUGIN_CB_R_REGS, (void *)(uintptr_t)pc);
        }
        if (pc >= start_pc && pc <= end_pc) {
            qemu_plugin_register_vcpu_mem_cb(
                insn, memory_access, QEMU_PLUGIN_CB_NO_REGS,
                QEMU_PLUGIN_MEM_RW, (void *)(uintptr_t)pc);
        }
    }
}

static void vcpu_init(unsigned int cpu_index, void *userdata)
{
    g_autoptr(GArray) available = qemu_plugin_get_registers();

    (void)cpu_index;
    (void)userdata;
    if (registers->len) {
        return;
    }
    for (size_t i = 0; i < available->len; i++) {
        qemu_plugin_reg_descriptor *desc =
            &g_array_index(available, qemu_plugin_reg_descriptor, i);
        Register *reg = g_new0(Register, 1);

        reg->handle = desc->handle;
        reg->name = g_strdup(desc->name);
        g_ptr_array_add(registers, reg);
    }
}

static void at_exit(void *userdata)
{
    (void)userdata;
    g_mutex_lock(&trace_lock);
    if (!completed) {
        write_footer(false);
    }
    fclose(trace_file);
    trace_file = NULL;
    g_mutex_unlock(&trace_lock);
    g_ptr_array_free(registers, true);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    const char *out_path = NULL;

    (void)info;
    for (int i = 0; i < argc; i++) {
        if (g_str_has_prefix(argv[i], "out=")) {
            out_path = argv[i] + 4;
        } else if (g_str_has_prefix(argv[i], "start=")) {
            start_pc = g_ascii_strtoull(argv[i] + 6, NULL, 0);
        } else if (g_str_has_prefix(argv[i], "end=")) {
            end_pc = g_ascii_strtoull(argv[i] + 4, NULL, 0);
        } else if (g_str_has_prefix(argv[i], "exit=")) {
            exit_pc = g_ascii_strtoull(argv[i] + 5, NULL, 0);
        } else {
            fprintf(stderr, "unknown audio-contract option: %s\n", argv[i]);
            return -1;
        }
    }
    if (!out_path || !*out_path || !start_pc || end_pc < start_pc || !exit_pc) {
        fprintf(stderr, "audio-contract requires out=PATH and valid PCs\n");
        return -1;
    }
    trace_file = fopen(out_path, "w");
    if (!trace_file) {
        perror("audio-contract output");
        return -1;
    }
    registers = g_ptr_array_new_with_free_func(register_free);
    fprintf(trace_file,
            "{\"kind\":\"header\",\"schema_version\":1,"
            "\"start_pc\":\"0x%08" PRIx64 "\","
            "\"end_pc\":\"0x%08" PRIx64 "\","
            "\"exit_pc\":\"0x%08" PRIx64 "\",\"defaults\":%s}\n",
            start_pc, end_pc, exit_pc,
            start_pc == default_start_pc && end_pc == default_end_pc &&
                    exit_pc == default_exit_pc ? "true" : "false");
    fflush(trace_file);
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate, NULL);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
