/*
 * Same-process replay verifier for the AR MKII native audio kernel.
 *
 * Call 1 discovers the data footprint. Call 2 snapshots that footprint and
 * every writable register, runs natively, restores the entry state, and jumps
 * back to the kernel entry. Call 3 is therefore a shadow execution from the
 * identical state. Its access values and exit state must match call 2 exactly.
 *
 * Only aggregate results are written. The output contains no register values,
 * guest addresses, firmware bytes, or PCM.
 */
#include <glib.h>
#include <inttypes.h>
#include <qemu-plugin.h>
#include <stdio.h>
#include <string.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef enum {
    PHASE_DISCOVERY,
    PHASE_WAIT_NATIVE,
    PHASE_NATIVE,
    PHASE_SHADOW,
    PHASE_DONE,
} Phase;

typedef struct {
    struct qemu_plugin_register *handle;
    bool readonly;
    GByteArray *entry;
    GByteArray *native_exit;
} RegisterState;

typedef struct {
    uint64_t address;
    uint8_t entry;
    uint8_t native_exit;
} ByteState;

typedef struct {
    uint64_t pc;
    uint64_t address;
    qemu_plugin_mem_value value;
    unsigned size;
    bool store;
} Access;

static uint64_t start_pc = 0x401184c4;
static uint64_t end_pc = 0x401187ff;
static uint64_t exit_pc = 0x40117fc2;
static FILE *report_file;
static GPtrArray *registers;
static GHashTable *footprint;
static GPtrArray *bytes;
static GArray *native_accesses;
static GArray *shadow_accesses;
static GMutex lock;
static Phase phase = PHASE_DISCOVERY;
static bool active;
static bool footprint_miss;
static bool snapshot_error;
static bool restore_error;
static bool reported;
static bool access_match;
static bool register_match;
static bool memory_match;
static uint64_t discovery_events;

static void register_state_free(gpointer data)
{
    RegisterState *reg = data;

    g_byte_array_unref(reg->entry);
    g_byte_array_unref(reg->native_exit);
    g_free(reg);
}

static void byte_state_free(gpointer data)
{
    g_free(data);
}

static void add_footprint(uint64_t address, unsigned size)
{
    for (unsigned i = 0; i < size; i++) {
        uint64_t byte_address = address + i;

        if (!g_hash_table_lookup(footprint, &byte_address)) {
            ByteState *state = g_new0(ByteState, 1);

            state->address = byte_address;
            g_hash_table_insert(footprint, &state->address, state);
            g_ptr_array_add(bytes, state);
        }
    }
}

static bool footprint_contains(uint64_t address, unsigned size)
{
    for (unsigned i = 0; i < size; i++) {
        uint64_t byte_address = address + i;

        if (!g_hash_table_lookup(footprint, &byte_address)) {
            return false;
        }
    }
    return true;
}

static bool snapshot_memory(bool at_entry)
{
    g_autoptr(GByteArray) value = g_byte_array_new();
    bool ok = true;

    for (size_t i = 0; i < bytes->len; i++) {
        ByteState *state = g_ptr_array_index(bytes, i);

        g_byte_array_set_size(value, 0);
        if (!qemu_plugin_read_memory_vaddr(state->address, value, 1) ||
            value->len != 1) {
            ok = false;
            continue;
        }
        if (at_entry) {
            state->entry = value->data[0];
        } else {
            state->native_exit = value->data[0];
        }
    }
    return ok;
}

static bool restore_memory(bool to_entry)
{
    g_autoptr(GByteArray) value = g_byte_array_sized_new(1);
    bool ok = true;

    for (size_t i = 0; i < bytes->len; i++) {
        ByteState *state = g_ptr_array_index(bytes, i);
        uint8_t byte = to_entry ? state->entry : state->native_exit;

        g_byte_array_set_size(value, 0);
        g_byte_array_append(value, &byte, 1);
        if (!qemu_plugin_write_memory_vaddr(state->address, value)) {
            ok = false;
        }
    }
    return ok;
}

static bool snapshot_registers(bool at_entry)
{
    bool ok = true;

    for (size_t i = 0; i < registers->len; i++) {
        RegisterState *reg = g_ptr_array_index(registers, i);
        GByteArray *value = at_entry ? reg->entry : reg->native_exit;

        g_byte_array_set_size(value, 0);
        if (!qemu_plugin_read_register(reg->handle, value) || !value->len) {
            ok = false;
        }
    }
    return ok;
}

static bool restore_registers(bool to_entry)
{
    bool ok = true;

    for (size_t i = 0; i < registers->len; i++) {
        RegisterState *reg = g_ptr_array_index(registers, i);
        GByteArray *value = to_entry ? reg->entry : reg->native_exit;

        if (!reg->readonly &&
            !qemu_plugin_write_register(reg->handle, value)) {
            ok = false;
        }
    }
    return ok;
}

static bool compare_registers(void)
{
    g_autoptr(GByteArray) value = g_byte_array_new();

    for (size_t i = 0; i < registers->len; i++) {
        RegisterState *reg = g_ptr_array_index(registers, i);

        g_byte_array_set_size(value, 0);
        if (!qemu_plugin_read_register(reg->handle, value) ||
            value->len != reg->native_exit->len ||
            memcmp(value->data, reg->native_exit->data, value->len)) {
            return false;
        }
    }
    return true;
}

static bool compare_memory(void)
{
    g_autoptr(GByteArray) value = g_byte_array_new();

    for (size_t i = 0; i < bytes->len; i++) {
        ByteState *state = g_ptr_array_index(bytes, i);

        g_byte_array_set_size(value, 0);
        if (!qemu_plugin_read_memory_vaddr(state->address, value, 1) ||
            value->len != 1 || value->data[0] != state->native_exit) {
            return false;
        }
    }
    return true;
}

static bool access_equal(const Access *a, const Access *b)
{
    if (a->pc != b->pc || a->address != b->address ||
        a->size != b->size || a->store != b->store ||
        a->value.type != b->value.type) {
        return false;
    }
    switch (a->value.type) {
    case QEMU_PLUGIN_MEM_VALUE_U8:
        return a->value.data.u8 == b->value.data.u8;
    case QEMU_PLUGIN_MEM_VALUE_U16:
        return a->value.data.u16 == b->value.data.u16;
    case QEMU_PLUGIN_MEM_VALUE_U32:
        return a->value.data.u32 == b->value.data.u32;
    case QEMU_PLUGIN_MEM_VALUE_U64:
        return a->value.data.u64 == b->value.data.u64;
    case QEMU_PLUGIN_MEM_VALUE_U128:
        return a->value.data.u128.low == b->value.data.u128.low &&
               a->value.data.u128.high == b->value.data.u128.high;
    default:
        return false;
    }
}

static bool compare_accesses(void)
{
    if (native_accesses->len != shadow_accesses->len) {
        return false;
    }
    for (size_t i = 0; i < native_accesses->len; i++) {
        Access *native = &g_array_index(native_accesses, Access, i);
        Access *shadow = &g_array_index(shadow_accesses, Access, i);

        if (!access_equal(native, shadow)) {
            return false;
        }
    }
    return true;
}

static void write_report(bool complete)
{
    bool pass = complete && !footprint_miss && !snapshot_error &&
                !restore_error && access_match && register_match && memory_match;

    fprintf(report_file,
            "{\"schema_version\":1,\"complete\":%s,"
            "\"status\":\"%s\",\"discovery_events\":%" PRIu64
            ",\"footprint_bytes\":%u,\"registers\":%u,"
            "\"native_events\":%u,\"shadow_events\":%u,"
            "\"footprint_miss\":%s,\"snapshot_error\":%s,"
            "\"restore_error\":%s,\"access_match\":%s,"
            "\"register_match\":%s,\"memory_match\":%s}\n",
            complete ? "true" : "false",
            pass ? "PASS_IDENTICAL_NATIVE_SHADOW" : "FAIL",
            discovery_events, bytes->len, registers->len,
            native_accesses->len, shadow_accesses->len,
            footprint_miss ? "true" : "false",
            snapshot_error ? "true" : "false",
            restore_error ? "true" : "false",
            access_match ? "true" : "false",
            register_match ? "true" : "false",
            memory_match ? "true" : "false");
    fflush(report_file);
    reported = true;
}

static void memory_access(unsigned int cpu_index, qemu_plugin_meminfo_t info,
                          uint64_t address, void *userdata)
{
    uint64_t pc = (uintptr_t)userdata;
    unsigned size = 1u << qemu_plugin_mem_size_shift(info);

    (void)cpu_index;
    g_mutex_lock(&lock);
    if (!active) {
        g_mutex_unlock(&lock);
        return;
    }
    if (phase == PHASE_DISCOVERY) {
        add_footprint(address, size);
        discovery_events++;
    } else if (phase == PHASE_NATIVE || phase == PHASE_SHADOW) {
        Access access = {
            .pc = pc,
            .address = address,
            .value = qemu_plugin_mem_get_value(info),
            .size = size,
            .store = qemu_plugin_mem_is_store(info),
        };

        if (!footprint_contains(address, size)) {
            footprint_miss = true;
        }
        g_array_append_val(phase == PHASE_NATIVE ? native_accesses :
                           shadow_accesses, access);
    }
    g_mutex_unlock(&lock);
}

static void boundary(unsigned int cpu_index, void *userdata)
{
    uint64_t pc = (uintptr_t)userdata;

    (void)cpu_index;
    g_mutex_lock(&lock);
    if (pc == start_pc) {
        if (phase == PHASE_DISCOVERY) {
            active = true;
        } else if (phase == PHASE_WAIT_NATIVE) {
            snapshot_error |= !snapshot_memory(true);
            snapshot_error |= !snapshot_registers(true);
            phase = PHASE_NATIVE;
            active = true;
        } else if (phase == PHASE_SHADOW) {
            active = true;
        }
        g_mutex_unlock(&lock);
        return;
    }
    if (pc != exit_pc || !active) {
        g_mutex_unlock(&lock);
        return;
    }

    active = false;
    if (phase == PHASE_DISCOVERY) {
        phase = PHASE_WAIT_NATIVE;
        g_mutex_unlock(&lock);
        return;
    }
    if (phase == PHASE_NATIVE) {
        snapshot_error |= !snapshot_memory(false);
        snapshot_error |= !snapshot_registers(false);
        if (footprint_miss || snapshot_error) {
            phase = PHASE_DONE;
            write_report(true);
            g_mutex_unlock(&lock);
            return;
        }
        restore_error |= !restore_memory(true);
        restore_error |= !restore_registers(true);
        if (restore_error) {
            restore_memory(false);
            restore_registers(false);
            phase = PHASE_DONE;
            write_report(true);
            g_mutex_unlock(&lock);
            return;
        }
        phase = PHASE_SHADOW;
        g_mutex_unlock(&lock);
        qemu_plugin_set_pc(start_pc);
    }
    if (phase == PHASE_SHADOW) {
        access_match = compare_accesses();
        register_match = compare_registers();
        memory_match = compare_memory();
        restore_error |= !restore_memory(false);
        restore_error |= !restore_registers(false);
        phase = PHASE_DONE;
        write_report(true);
    }
    g_mutex_unlock(&lock);
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
                insn, boundary, QEMU_PLUGIN_CB_RW_REGS, (void *)(uintptr_t)pc);
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
        RegisterState *reg = g_new0(RegisterState, 1);

        reg->handle = desc->handle;
        reg->readonly = desc->is_readonly;
        reg->entry = g_byte_array_new();
        reg->native_exit = g_byte_array_new();
        g_ptr_array_add(registers, reg);
    }
}

static void at_exit(void *userdata)
{
    (void)userdata;
    g_mutex_lock(&lock);
    if (!reported) {
        write_report(false);
    }
    fclose(report_file);
    g_mutex_unlock(&lock);
    g_ptr_array_free(registers, true);
    g_hash_table_destroy(footprint);
    g_ptr_array_free(bytes, true);
    g_array_free(native_accesses, true);
    g_array_free(shadow_accesses, true);
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
            fprintf(stderr, "unknown audio-shadow option: %s\n", argv[i]);
            return -1;
        }
    }
    if (!out_path || !*out_path || !start_pc || end_pc < start_pc || !exit_pc) {
        fprintf(stderr, "audio-shadow requires out=PATH and valid PCs\n");
        return -1;
    }
    report_file = fopen(out_path, "w");
    if (!report_file) {
        perror("audio-shadow output");
        return -1;
    }
    registers = g_ptr_array_new_with_free_func(register_state_free);
    footprint = g_hash_table_new(g_int64_hash, g_int64_equal);
    bytes = g_ptr_array_new_with_free_func(byte_state_free);
    native_accesses = g_array_new(false, false, sizeof(Access));
    shadow_accesses = g_array_new(false, false, sizeof(Access));
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate, NULL);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
