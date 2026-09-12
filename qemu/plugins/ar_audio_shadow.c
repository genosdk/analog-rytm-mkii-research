/*
 * Same-process replay verifier for the AR MKII native audio kernel.
 *
 * Natural calls discover touched 4 KiB pages until the footprint stabilizes.
 * The next call snapshots those pages and every writable register, runs
 * natively, restores the entry state, and jumps back to the kernel entry. That
 * replay is either the native code again or an optional accelerator candidate
 * from identical state. Native shadow access values must match exactly; an
 * optimized candidate must match complete register and touched-memory state.
 * The same guarded implementation can run on every matching call with
 * runtime=inner; transactional stores and register rollback preserve native
 * fallback if an operation cannot be completed.
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
    char *name;
    bool readonly;
    GByteArray *entry;
    GByteArray *native_exit;
} RegisterState;

typedef struct {
    uint64_t base;
    GByteArray *entry;
    GByteArray *native_exit;
} PageState;

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
static const uint64_t page_mask = ~(uint64_t)0xfff;
static const size_t page_size = 4096;
static FILE *report_file;
static GPtrArray *registers;
static struct qemu_plugin_register *tcg_control_register;
static struct qemu_plugin_register *tcg_transform_control_register;
static GHashTable *footprint;
static GHashTable *touched_bytes;
static GPtrArray *pages;
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
static uint64_t discovery_calls;
static uint64_t stable_discovery_calls;
static uint64_t required_stable_calls = 8;
static bool footprint_grew;
static bool candidate_inner;
static bool candidate_tcg;
static bool candidate_transform;
static bool candidate_tcg_transform;
static bool candidate_outer;
static bool candidate_attempted;
static bool candidate_executed;
static bool candidate_fallback;
static bool runtime_inner;
static uint64_t runtime_attempts;
static uint64_t runtime_executed;
static uint64_t runtime_fallbacks;

#define INNER_START_PC 0x401185ec
#define INNER_END_PC   0x40118668
#define INNER_EXIT_PC  0x4011866c
#define TRANSFORM_START_PC 0x4011c56e
#define TRANSFORM_END_PC   0x4011c596
#define TRANSFORM_EXIT_PC  0x4011c598
#define OUTER_START_PC 0x401184c4
#define OUTER_END_PC   0x401184f6
#define OUTER_EXIT_PC  0x401184f8
#define MACSR_PAV0     0x100
#define MACSR_OMC      0x080
#define MACSR_SU       0x040
#define MACSR_FI       0x020
#define MACSR_RT       0x010
#define MACSR_N        0x008
#define MACSR_Z        0x004
#define MACSR_V        0x002
#define MACSR_EV       0x001
#define CCF_Z          0x004
#define CCF_N          0x008

typedef struct {
    uint32_t d[8];
    uint32_t a[8];
    uint64_t macc[4];
    uint32_t macsr;
    uint32_t mask;
    uint32_t ps;
} InnerState;

typedef struct {
    uint16_t insn;
    uint16_t ext;
    int16_t displacement;
} InnerMac;

typedef struct {
    uint32_t address;
    uint32_t original;
    uint32_t value;
} InnerWrite;

typedef struct {
    InnerWrite item[1024];
    size_t count;
} InnerWrites;

static const InnerMac inner_before_acc0[] = {
    { 0xa8d9, 0xc805,   0 },
    { 0xa8e9, 0xc800,   4 },
    { 0xa840, 0x0810,   0 },
    { 0xa8e9, 0xc801,  12 },
    { 0xa841, 0x0810,   0 },
    { 0xa8e9, 0xc802,  20 },
    { 0xac99, 0xc812,   0 },
    { 0xa8e9, 0xc803,  24 },
    { 0xa8ee, 0xc813, -16 },
    { 0xa8ae, 0xc903, -12 },
    { 0xaeae, 0x4902,  -8 },
    { 0xa8ee, 0x7901,  -4 },
    { 0xac99, 0xc900,   0 },
    { 0xac99, 0x4913,   0 },
    { 0xaca9, 0x7912, -20 },
    { 0xa8e9, 0xc911,  -8 },
};

static const InnerMac inner_after_acc0 = { 0xa8a9, 0x7910, -12 };

static void register_state_free(gpointer data)
{
    RegisterState *reg = data;

    g_free(reg->name);
    g_byte_array_unref(reg->entry);
    g_byte_array_unref(reg->native_exit);
    g_free(reg);
}

static RegisterState *find_register(const char *name)
{
    for (size_t i = 0; i < registers->len; i++) {
        RegisterState *reg = g_ptr_array_index(registers, i);

        if (!strcmp(reg->name, name)) {
            return reg;
        }
    }
    return NULL;
}

static bool read_register_value(const char *name, uint64_t *result,
                                size_t expected_size)
{
    RegisterState *reg = find_register(name);
    g_autoptr(GByteArray) value = g_byte_array_new();

    if (!reg || !qemu_plugin_read_register(reg->handle, value) ||
        value->len != expected_size) {
        return false;
    }
    if (expected_size == 4) {
        uint32_t raw;

        memcpy(&raw, value->data, sizeof(raw));
        *result = GUINT32_FROM_BE(raw);
    } else if (expected_size == 8) {
        uint64_t raw;

        memcpy(&raw, value->data, sizeof(raw));
        *result = GUINT64_FROM_BE(raw);
    } else {
        return false;
    }
    return true;
}

static bool write_register_value(const char *name, uint64_t raw,
                                 size_t size)
{
    RegisterState *reg = find_register(name);
    g_autoptr(GByteArray) value = g_byte_array_sized_new(size);

    if (!reg || reg->readonly) {
        return false;
    }
    if (size == 4) {
        uint32_t be = GUINT32_TO_BE(raw);
        g_byte_array_append(value, (uint8_t *)&be, sizeof(be));
    } else if (size == 8) {
        uint64_t be = GUINT64_TO_BE(raw);
        g_byte_array_append(value, (uint8_t *)&be, sizeof(be));
    } else {
        return false;
    }
    return qemu_plugin_write_register(reg->handle, value);
}

static bool write_tcg_control(uint32_t raw)
{
    g_autoptr(GByteArray) value = g_byte_array_sized_new(4);
    uint32_t be = GUINT32_TO_BE(raw);

    if (!tcg_control_register) {
        return false;
    }
    g_byte_array_append(value, (uint8_t *)&be, sizeof(be));
    return qemu_plugin_write_register(tcg_control_register, value);
}

static bool write_tcg_transform_control(uint32_t raw)
{
    g_autoptr(GByteArray) value = g_byte_array_sized_new(4);
    uint32_t be = GUINT32_TO_BE(raw);

    if (!tcg_transform_control_register) {
        return false;
    }
    g_byte_array_append(value, (uint8_t *)&be, sizeof(be));
    return qemu_plugin_write_register(tcg_transform_control_register, value);
}

static bool read_tcg_control(uint32_t *result)
{
    g_autoptr(GByteArray) value = g_byte_array_new();
    uint32_t raw;

    if (!tcg_control_register ||
        !qemu_plugin_read_register(tcg_control_register, value) ||
        value->len != sizeof(raw)) {
        return false;
    }
    memcpy(&raw, value->data, sizeof(raw));
    *result = GUINT32_FROM_BE(raw);
    return true;
}

static bool read_tcg_transform_control(uint32_t *result)
{
    g_autoptr(GByteArray) value = g_byte_array_new();
    uint32_t raw;

    if (!tcg_transform_control_register ||
        !qemu_plugin_read_register(tcg_transform_control_register, value) ||
        value->len != sizeof(raw)) {
        return false;
    }
    memcpy(&raw, value->data, sizeof(raw));
    *result = GUINT32_FROM_BE(raw);
    return true;
}

static bool read_memory_u32(uint32_t address, uint32_t *result)
{
    g_autoptr(GByteArray) value = g_byte_array_new();

    if (!qemu_plugin_read_memory_vaddr(address, value, 4) || value->len != 4) {
        return false;
    }
    memcpy(result, value->data, sizeof(*result));
    *result = GUINT32_FROM_BE(*result);
    return true;
}

static bool write_memory_u32(uint32_t address, uint32_t raw)
{
    g_autoptr(GByteArray) value = g_byte_array_sized_new(4);
    uint32_t be = GUINT32_TO_BE(raw);

    g_byte_array_append(value, (uint8_t *)&be, sizeof(be));
    return qemu_plugin_write_memory_vaddr(address, value);
}

static bool inner_read_memory(const InnerWrites *writes, uint32_t address,
                              uint32_t *result)
{
    for (size_t i = writes->count; i > 0; i--) {
        if (writes->item[i - 1].address == address) {
            *result = writes->item[i - 1].value;
            return true;
        }
    }
    return read_memory_u32(address, result);
}

static bool inner_queue_write(InnerWrites *writes, uint32_t address,
                              uint32_t value)
{
    for (size_t i = 0; i < writes->count; i++) {
        if (writes->item[i].address == address) {
            writes->item[i].value = value;
            return true;
        }
    }
    if (writes->count == G_N_ELEMENTS(writes->item) ||
        !read_memory_u32(address, &writes->item[writes->count].original)) {
        return false;
    }
    writes->item[writes->count].address = address;
    writes->item[writes->count].value = value;
    writes->count++;
    return true;
}

static bool inner_apply_writes(const InnerWrites *writes, size_t *applied)
{
    *applied = 0;
    for (; *applied < writes->count; (*applied)++) {
        const InnerWrite *write = &writes->item[*applied];

        if (!write_memory_u32(write->address, write->value)) {
            return false;
        }
    }
    return true;
}

static bool inner_rollback_writes(const InnerWrites *writes, size_t applied)
{
    bool ok = true;

    while (applied > 0) {
        const InnerWrite *write = &writes->item[--applied];

        ok &= write_memory_u32(write->address, write->original);
    }
    return ok;
}

static void inner_mac_clear_flags(InnerState *s)
{
    s->macsr &= ~(MACSR_N | MACSR_Z | MACSR_V | MACSR_EV);
}

static void inner_mac_set_flags(InnerState *s, unsigned acc)
{
    uint64_t value = s->macc[acc];
    int64_t upper;

    if (!value) {
        s->macsr |= MACSR_Z;
    } else if (value & (UINT64_C(1) << 47)) {
        s->macsr |= MACSR_N;
    }
    if (s->macsr & (MACSR_PAV0 << acc)) {
        s->macsr |= MACSR_V;
    }
    upper = (int64_t)value >> 40;
    if (upper != 0 && upper != -1) {
        s->macsr |= MACSR_EV;
    }
}

static void inner_mac_saturate_fractional(InnerState *s, unsigned acc)
{
    int64_t sum = (int64_t)s->macc[acc];
    int64_t result = (int64_t)(s->macc[acc] << 16) >> 16;

    if (result != sum) {
        s->macsr |= MACSR_V;
    }
    if (s->macsr & MACSR_V) {
        s->macsr |= MACSR_PAV0 << acc;
        if (s->macsr & MACSR_OMC) {
            result = (result >> 63) ^ INT64_C(0x7fffffffffff);
        }
    }
    s->macc[acc] = result;
}

static bool inner_mac(InnerState *s, InnerWrites *writes, const InnerMac *op)
{
    uint32_t rx;
    uint32_t ry;
    uint32_t loaded = 0;
    uint32_t address = 0;
    uint64_t product;
    unsigned acc = ((op->insn >> 7) & 1) | ((op->ext >> 3) & 2);
    bool load = (op->insn & 0x30) != 0;

    if (!(op->ext & 0x0800) ||
        (!load && (op->ext & 3)) ||
        (load && ((op->insn >> 3) & 7) != 3 &&
         ((op->insn >> 3) & 7) != 5)) {
        return false;
    }
    if (load) {
        unsigned mode = (op->insn >> 3) & 7;
        unsigned areg = op->insn & 7;

        address = s->a[areg];
        if (mode == 5) {
            address += op->displacement;
        }
        address &= s->mask;
        if (!inner_read_memory(writes, address, &loaded)) {
            return false;
        }
        acc ^= 1;
        rx = (op->ext & 0x8000) ? s->a[(op->ext >> 12) & 7] :
                                  s->d[(op->ext >> 12) & 7];
        ry = (op->ext & 8) ? s->a[op->ext & 7] : s->d[op->ext & 7];
    } else {
        rx = (op->insn & 0x40) ? s->a[(op->insn >> 9) & 7] :
                                  s->d[(op->insn >> 9) & 7];
        ry = (op->insn & 8) ? s->a[op->insn & 7] :
                              s->d[op->insn & 7];
    }

    inner_mac_clear_flags(s);
    product = (uint64_t)(((int64_t)(int32_t)rx * (int32_t)ry) >> 23);
    if (op->ext & 0x100) {
        s->macc[acc] -= product;
    } else {
        s->macc[acc] += product;
    }
    inner_mac_saturate_fractional(s, acc);
    inner_mac_set_flags(s, acc);

    if (load) {
        unsigned reg = (op->insn >> 9) & 7;

        if (op->insn & 0x40) {
            s->a[reg] = loaded;
        } else {
            s->d[reg] = loaded;
        }
        if (((op->insn >> 3) & 7) == 3) {
            s->a[op->insn & 7] = address + 4;
        }
    }
    return true;
}

static uint32_t inner_movclr(InnerState *s, unsigned acc)
{
    uint32_t result = s->macc[acc] >> 8;

    s->macc[acc] = 0;
    s->macsr &= ~(MACSR_PAV0 << acc);
    return result;
}

static uint32_t inner_saturating_add(uint32_t left, uint32_t right)
{
    uint32_t result = left + right;

    if ((~(left ^ right) & (left ^ result) & 0x80000000U) != 0) {
        return (result & 0x80000000U) ? 0x7fffffffU : 0x80000000U;
    }
    return result;
}

static bool read_inner_state(InnerState *s)
{
    static const char *dnames[] = {
        "d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7"
    };
    static const char *anames[] = {
        "a0", "a1", "a2", "a3", "a4", "a5", "fp", "sp"
    };
    static const char *mnames[] = {
        "macc0_raw", "macc1_raw", "macc2_raw", "macc3_raw"
    };
    uint64_t value;

    for (size_t i = 0; i < G_N_ELEMENTS(dnames); i++) {
        if (!read_register_value(dnames[i], &value, 4)) {
            return false;
        }
        s->d[i] = value;
    }
    for (size_t i = 0; i < G_N_ELEMENTS(anames); i++) {
        if (!read_register_value(anames[i], &value, 4)) {
            return false;
        }
        s->a[i] = value;
    }
    for (size_t i = 0; i < G_N_ELEMENTS(mnames); i++) {
        if (!read_register_value(mnames[i], &value, 8)) {
            return false;
        }
        s->macc[i] = value;
    }
    if (!read_register_value("macsr", &value, 4)) {
        return false;
    }
    s->macsr = value;
    if (!read_register_value("mac_mask", &value, 4)) {
        return false;
    }
    s->mask = value;
    if (!read_register_value("ps", &value, 4)) {
        return false;
    }
    s->ps = value;
    return true;
}

static bool write_inner_state(const InnerState *s)
{
    static const char *dnames[] = {
        "d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7"
    };
    static const char *anames[] = {
        "a0", "a1", "a2", "a3", "a4", "a5", "fp", "sp"
    };
    static const char *mnames[] = {
        "macc0_raw", "macc1_raw", "macc2_raw", "macc3_raw"
    };

    for (size_t i = 0; i < G_N_ELEMENTS(dnames); i++) {
        if (!write_register_value(dnames[i], s->d[i], 4)) {
            return false;
        }
    }
    for (size_t i = 0; i < G_N_ELEMENTS(anames); i++) {
        if (!write_register_value(anames[i], s->a[i], 4)) {
            return false;
        }
    }
    for (size_t i = 0; i < G_N_ELEMENTS(mnames); i++) {
        if (!write_register_value(mnames[i], s->macc[i], 8)) {
            return false;
        }
    }
    return write_register_value("macsr", s->macsr, 4) &&
           write_register_value("mac_mask", s->mask, 4) &&
           write_register_value("ps", s->ps, 4);
}

static bool accelerate_inner(void)
{
    InnerState s;
    InnerState entry;
    InnerWrites writes = { 0 };
    size_t applied = 0;

    if (!read_inner_state(&s) ||
        (s.macsr & (MACSR_OMC | MACSR_SU | MACSR_FI | MACSR_RT)) != MACSR_FI ||
        s.mask != UINT32_MAX) {
        return false;
    }
    entry = s;

    if (!inner_read_memory(&writes, s.a[1], &s.a[4])) {
        return false;
    }
    s.a[1] += 8;
    s.a[7] -= 4;
    if (!inner_queue_write(&writes, s.a[7], 16)) {
        return false;
    }

    for (unsigned iteration = 0; iteration < 16; iteration++) {
        s.macc[2] = (uint64_t)((int64_t)(int32_t)s.a[4] * 256);
        s.macsr &= ~(MACSR_PAV0 << 2);
        inner_mac_clear_flags(&s);
        inner_mac_set_flags(&s, 2);

        for (size_t i = 0; i < G_N_ELEMENTS(inner_before_acc0); i++) {
            if (!inner_mac(&s, &writes, &inner_before_acc0[i])) {
                return false;
            }
        }
        s.d[7] = inner_movclr(&s, 0);
        if (!inner_queue_write(&writes, s.a[6], s.d[7])) {
            return false;
        }
        s.a[6] += 4;

        if (!inner_mac(&s, &writes, &inner_after_acc0)) {
            return false;
        }
        s.d[6] = inner_saturating_add(s.d[6], s.d[7]);
        if (!inner_queue_write(&writes, s.a[2], s.d[6])) {
            return false;
        }
        s.a[2] += 4;

        s.d[7] = inner_movclr(&s, 2);
        if (!inner_queue_write(&writes, s.a[6], s.d[7])) {
            return false;
        }
        s.a[6] += 4;
        s.d[4] = inner_saturating_add(s.d[4], s.d[7]);
        if (!inner_queue_write(&writes, s.a[2], s.d[4])) {
            return false;
        }
        s.a[2] += 4;
        if (!inner_queue_write(&writes, s.a[7], 15 - iteration)) {
            return false;
        }
    }
    s.ps = (s.ps & ~0x1fU) | CCF_Z;
    if (!inner_apply_writes(&writes, &applied)) {
        inner_rollback_writes(&writes, applied);
        return false;
    }
    if (!write_inner_state(&s)) {
        bool rollback_ok = inner_rollback_writes(&writes, applied);

        rollback_ok &= write_inner_state(&entry);
        (void)rollback_ok;
        return false;
    }
    return true;
}

static uint32_t transform_word_operand(uint32_t value, bool upper)
{
    return upper ? value & 0xffff0000U : value << 16;
}

static void transform_mac(InnerState *s, unsigned acc,
                          uint32_t left, uint32_t right)
{
    uint64_t product;

    inner_mac_clear_flags(s);
    product = (uint64_t)(((int64_t)(int32_t)left * (int32_t)right) >> 23);
    s->macc[acc] += product;
    inner_mac_saturate_fractional(s, acc);
    inner_mac_set_flags(s, acc);
}

static bool accelerate_transform(void)
{
    InnerState s;
    InnerState entry;
    InnerWrites writes = { 0 };
    size_t applied = 0;

    if (!read_inner_state(&s) ||
        (s.macsr & (MACSR_OMC | MACSR_SU | MACSR_FI | MACSR_RT)) != MACSR_FI ||
        s.mask != UINT32_MAX || s.d[3] != 572) {
        return false;
    }
    entry = s;

    for (unsigned iteration = 0; iteration < 286; iteration++) {
        uint32_t loaded;

        if (!inner_read_memory(&writes, s.a[2] & s.mask, &loaded)) {
            return false;
        }
        transform_mac(&s, 0,
                      transform_word_operand(s.d[0], true),
                      transform_word_operand(s.d[7], true));
        s.d[4] = loaded;
        transform_mac(&s, 0, s.d[4], s.d[6]);

        if (!inner_queue_write(&writes, s.a[5], s.d[1])) {
            return false;
        }
        s.a[5] += 4;

        if (!inner_read_memory(&writes, (s.a[2] + 4) & s.mask, &loaded)) {
            return false;
        }
        transform_mac(&s, 1,
                      transform_word_operand(s.d[0], false),
                      transform_word_operand(s.d[7], true));
        s.d[0] = loaded;

        if (!inner_read_memory(&writes, s.a[1] & s.mask, &loaded)) {
            return false;
        }
        transform_mac(&s, 1, s.d[0], s.d[6]);
        s.d[0] = loaded;
        s.a[1] += 4;

        s.d[1] = inner_movclr(&s, 0);
        if (!inner_queue_write(&writes, s.a[2], s.d[1])) {
            return false;
        }
        s.a[2] += 4;
        s.d[4] = inner_movclr(&s, 1);
        if (!inner_queue_write(&writes, s.a[2], s.d[4])) {
            return false;
        }
        s.a[2] += 4;

        s.d[4] += s.d[2];
        s.d[4] = (s.d[4] << 16) | (s.d[4] >> 16);
        s.d[1] += s.d[2];
        s.d[1] = (s.d[1] & 0xffff0000U) | (s.d[4] & 0xffffU);
        s.d[3] -= 2;
    }
    if (!inner_queue_write(&writes, s.a[5], s.d[1])) {
        return false;
    }
    s.a[5] += 4;
    s.ps = (s.ps & ~0x1fU) |
           (s.d[1] == 0 ? CCF_Z : (s.d[1] & 0x80000000U ? CCF_N : 0));

    if (!inner_apply_writes(&writes, &applied)) {
        inner_rollback_writes(&writes, applied);
        return false;
    }
    if (!write_inner_state(&s)) {
        bool rollback_ok = inner_rollback_writes(&writes, applied);

        rollback_ok &= write_inner_state(&entry);
        (void)rollback_ok;
        return false;
    }
    return true;
}

static bool accelerate_outer(void)
{
    InnerState s;
    InnerState entry;
    InnerWrites writes = { 0 };
    size_t applied = 0;

    if (!read_inner_state(&s) ||
        (s.macsr & (MACSR_OMC | MACSR_SU | MACSR_FI | MACSR_RT)) != MACSR_FI ||
        s.mask != UINT32_MAX || s.d[4] != 64) {
        return false;
    }
    entry = s;

    for (unsigned iteration = 0; iteration < 64; iteration++) {
        uint32_t loaded;
        uint32_t sum;
        uint32_t carry;
        unsigned shift;

        if (!inner_queue_write(&writes, s.a[1], s.d[7])) {
            return false;
        }
        s.a[1] += 4;
        s.d[5] = s.d[3];

        if (!inner_read_memory(&writes, (s.a[5] + 1024) & s.mask,
                               &loaded)) {
            return false;
        }
        transform_mac(&s, 0,
                      transform_word_operand(s.a[6], true),
                      transform_word_operand(s.d[6], true));
        s.d[7] = loaded;

        if (!inner_read_memory(&writes, s.a[2] & s.mask, &loaded)) {
            return false;
        }
        transform_mac(&s, 0,
                      transform_word_operand(s.a[6], false),
                      transform_word_operand(s.d[6], false));
        s.a[6] = loaded;
        s.a[2] += 4;

        if (!inner_read_memory(&writes, s.a[2] & s.mask, &loaded)) {
            return false;
        }
        transform_mac(&s, 0,
                      transform_word_operand(s.d[7], true),
                      transform_word_operand(s.a[6], true));
        s.d[6] = loaded;
        s.a[2] += 4;

        shift = s.d[0] & 63;
        s.d[5] = shift >= 32 ? 0 : s.d[5] >> shift;
        s.a[2] = s.a[4] + s.d[1] * 2;

        if (!inner_read_memory(&writes, (s.a[5] + 2048) & s.mask,
                               &loaded)) {
            return false;
        }
        transform_mac(&s, 0,
                      transform_word_operand(s.d[7], false),
                      transform_word_operand(s.a[6], false));
        s.d[7] = loaded;
        s.a[5] = s.a[0] + s.d[5] * 4;

        if (!inner_read_memory(&writes, s.a[2] & s.mask, &loaded)) {
            return false;
        }
        transform_mac(&s, 0,
                      transform_word_operand(s.d[7], true),
                      transform_word_operand(s.d[6], true));
        s.a[6] = loaded;
        s.a[2] += 4;

        if (!inner_read_memory(&writes, s.a[5] & s.mask, &loaded)) {
            return false;
        }
        transform_mac(&s, 0,
                      transform_word_operand(s.d[7], false),
                      transform_word_operand(s.d[6], false));
        s.d[6] = loaded;

        if (!inner_read_memory(&writes, s.a[7], &loaded)) {
            return false;
        }
        sum = s.d[3] + loaded;
        carry = sum < s.d[3];
        s.d[3] = sum;
        s.d[1] += s.d[2] + carry;
        s.d[4]--;
        s.d[7] = inner_movclr(&s, 0);
    }
    s.ps = (s.ps & ~0x1fU) | CCF_Z;

    if (!inner_apply_writes(&writes, &applied)) {
        inner_rollback_writes(&writes, applied);
        return false;
    }
    if (!write_inner_state(&s)) {
        bool rollback_ok = inner_rollback_writes(&writes, applied);

        rollback_ok &= write_inner_state(&entry);
        (void)rollback_ok;
        return false;
    }
    return true;
}

static void page_state_free(gpointer data)
{
    PageState *page = data;

    g_byte_array_unref(page->entry);
    g_byte_array_unref(page->native_exit);
    g_free(page);
}

static bool add_footprint(uint64_t address, unsigned size)
{
    bool grew = false;
    uint64_t first = address & page_mask;
    uint64_t last = (address + size - 1) & page_mask;

    for (unsigned i = 0; i < size; i++) {
        uint64_t byte_address = address + i;

        if (!g_hash_table_contains(touched_bytes, &byte_address)) {
            uint64_t *key = g_new(uint64_t, 1);

            *key = byte_address;
            g_hash_table_add(touched_bytes, key);
        }
    }
    for (uint64_t base = first; base <= last; base += page_size) {
        if (!g_hash_table_lookup(footprint, &base)) {
            PageState *state = g_new0(PageState, 1);

            state->base = base;
            state->entry = g_byte_array_sized_new(page_size);
            state->native_exit = g_byte_array_sized_new(page_size);
            g_hash_table_insert(footprint, &state->base, state);
            g_ptr_array_add(pages, state);
            grew = true;
        }
    }
    return grew;
}

static bool footprint_contains(uint64_t address, unsigned size)
{
    uint64_t first = address & page_mask;
    uint64_t last = (address + size - 1) & page_mask;

    for (uint64_t base = first; base <= last; base += page_size) {
        if (!g_hash_table_lookup(footprint, &base)) {
            return false;
        }
    }
    return true;
}

static bool snapshot_memory(bool at_entry)
{
    bool ok = true;

    for (size_t i = 0; i < pages->len; i++) {
        PageState *state = g_ptr_array_index(pages, i);
        GByteArray *value = at_entry ? state->entry : state->native_exit;

        g_byte_array_set_size(value, 0);
        if (!qemu_plugin_read_memory_vaddr(state->base, value, page_size) ||
            value->len != page_size) {
            ok = false;
        }
    }
    return ok;
}

static bool restore_memory(bool to_entry)
{
    bool ok = true;

    for (size_t i = 0; i < pages->len; i++) {
        PageState *state = g_ptr_array_index(pages, i);
        GByteArray *value = to_entry ? state->entry : state->native_exit;

        if (!qemu_plugin_write_memory_vaddr(state->base, value)) {
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
        if (!qemu_plugin_read_register(reg->handle, value)) {
            return false;
        }
        if (candidate_executed && !strcmp(reg->name, "pc")) {
            uint32_t actual;

            if (value->len != sizeof(actual)) {
                return false;
            }
            memcpy(&actual, value->data, sizeof(actual));
            if (GUINT32_FROM_BE(actual) != exit_pc &&
                (value->len != reg->native_exit->len ||
                 memcmp(value->data, reg->native_exit->data, value->len))) {
                return false;
            }
        } else if (value->len != reg->native_exit->len ||
                   memcmp(value->data, reg->native_exit->data, value->len)) {
            return false;
        }
    }
    return true;
}

static bool compare_memory(void)
{
    g_autoptr(GByteArray) value = g_byte_array_new();

    for (size_t i = 0; i < pages->len; i++) {
        PageState *state = g_ptr_array_index(pages, i);

        g_byte_array_set_size(value, 0);
        if (!qemu_plugin_read_memory_vaddr(state->base, value, page_size) ||
            value->len != state->native_exit->len) {
            return false;
        }
        for (size_t offset = 0; offset < page_size; offset++) {
            uint64_t address = state->base + offset;

            if (g_hash_table_contains(touched_bytes, &address) &&
                value->data[offset] != state->native_exit->data[offset]) {
                return false;
            }
        }
    }
    return true;
}

static bool access_equal(const Access *a, const Access *b)
{
    if ((!candidate_tcg && !candidate_tcg_transform && a->pc != b->pc) ||
        a->address != b->address ||
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
    if (runtime_inner) {
        fprintf(report_file,
                "{\"schema_version\":1,\"complete\":%s,"
                "\"status\":\"RUNTIME_INNER_ACCELERATOR\","
                "\"candidate\":\"inner\",\"attempted\":%" PRIu64
                ",\"executed\":%" PRIu64 ",\"fallbacks\":%" PRIu64
                "}\n",
                complete ? "true" : "false", runtime_attempts,
                runtime_executed, runtime_fallbacks);
        fflush(report_file);
        reported = true;
        return;
    }
    bool candidate_ok = (!candidate_inner && !candidate_tcg &&
                         !candidate_transform && !candidate_tcg_transform &&
                         !candidate_outer) ||
                        candidate_executed;
    bool pass = complete && !footprint_miss && !snapshot_error &&
                !restore_error && access_match && register_match && memory_match &&
                candidate_ok;
    const char *status = pass ?
        (candidate_inner ? "PASS_NATIVE_INNER_CANDIDATE" :
         candidate_tcg ? "PASS_NATIVE_INNER_TCG" :
         candidate_transform ? "PASS_NATIVE_TRANSFORM_CANDIDATE" :
         candidate_tcg_transform ? "PASS_NATIVE_TRANSFORM_TCG" :
         candidate_outer ? "PASS_NATIVE_OUTER_CANDIDATE" :
                           "PASS_IDENTICAL_NATIVE_SHADOW") : "FAIL";

    fprintf(report_file,
            "{\"schema_version\":1,\"complete\":%s,"
            "\"status\":\"%s\",\"discovery_calls\":%" PRIu64
            ",\"stable_discovery_calls\":%" PRIu64
            ",\"required_stable_calls\":%" PRIu64
            ",\"discovery_events\":%" PRIu64
            ",\"footprint_pages\":%u,\"touched_bytes\":%u,"
            "\"snapshot_bytes\":%" PRIu64
            ",\"registers\":%u,"
            "\"native_events\":%u,\"shadow_events\":%u,"
            "\"candidate\":\"%s\",\"candidate_attempted\":%s,"
            "\"candidate_executed\":%s,\"candidate_fallback\":%s,"
            "\"access_comparison_applicable\":%s,"
            "\"footprint_miss\":%s,\"snapshot_error\":%s,"
            "\"restore_error\":%s,\"access_match\":%s,"
            "\"register_match\":%s,\"memory_match\":%s}\n",
            complete ? "true" : "false",
            status,
            discovery_calls, stable_discovery_calls, required_stable_calls,
            discovery_events, pages->len, g_hash_table_size(touched_bytes),
            (uint64_t)pages->len * page_size, registers->len,
            native_accesses->len, shadow_accesses->len,
            candidate_inner ? "inner" :
            candidate_tcg ? "tcg-inner" :
            candidate_transform ? "transform" :
            candidate_tcg_transform ? "tcg-transform" :
            candidate_outer ? "outer" : "native-shadow",
            candidate_attempted ? "true" : "false",
            candidate_executed ? "true" : "false",
            candidate_fallback ? "true" : "false",
            (candidate_inner || candidate_transform || candidate_outer) &&
            candidate_executed ?
            "false" : "true",
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
        footprint_grew |= add_footprint(address, size);
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
    bool redirect_candidate = false;

    (void)cpu_index;
    g_mutex_lock(&lock);
    if (runtime_inner) {
        if (pc == start_pc) {
            runtime_attempts++;
            if (accelerate_inner()) {
                runtime_executed++;
                redirect_candidate = true;
            } else {
                runtime_fallbacks++;
            }
        }
        g_mutex_unlock(&lock);
        if (redirect_candidate) {
            qemu_plugin_set_pc(exit_pc);
        }
        return;
    }
    if (pc == start_pc) {
        if (phase == PHASE_DISCOVERY) {
            footprint_grew = false;
            active = true;
        } else if (phase == PHASE_WAIT_NATIVE) {
            snapshot_error |= !snapshot_memory(true);
            snapshot_error |= !snapshot_registers(true);
            phase = PHASE_NATIVE;
            active = true;
        } else if (phase == PHASE_SHADOW) {
            active = true;
            if (candidate_inner || candidate_transform || candidate_outer) {
                candidate_attempted = true;
                candidate_executed = candidate_inner ? accelerate_inner() :
                                     candidate_transform ?
                                     accelerate_transform() :
                                     accelerate_outer();
                if (candidate_executed) {
                    redirect_candidate = true;
                } else {
                    candidate_fallback = true;
                    restore_error |= !restore_memory(true);
                    restore_error |= !restore_registers(true);
                }
            } else if (candidate_tcg || candidate_tcg_transform) {
                candidate_attempted = true;
            }
        }
        g_mutex_unlock(&lock);
        if (redirect_candidate) {
            qemu_plugin_set_pc(exit_pc);
        }
        return;
    }
    if (pc != exit_pc || !active) {
        g_mutex_unlock(&lock);
        return;
    }

    active = false;
    if (phase == PHASE_DISCOVERY) {
        discovery_calls++;
        if (footprint_grew) {
            stable_discovery_calls = 0;
        } else {
            stable_discovery_calls++;
        }
        if (stable_discovery_calls >= required_stable_calls) {
            phase = PHASE_WAIT_NATIVE;
        }
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
        if ((candidate_tcg && !write_tcg_control(2)) ||
            (candidate_tcg_transform &&
             !write_tcg_transform_control(2))) {
            candidate_fallback = true;
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
        if (candidate_tcg || candidate_tcg_transform) {
            uint32_t control;

            candidate_executed =
                (candidate_tcg ? read_tcg_control(&control) :
                 read_tcg_transform_control(&control)) && control == 0;
            candidate_fallback = !candidate_executed;
        }
        access_match = (candidate_inner || candidate_transform ||
                        candidate_outer) &&
                       candidate_executed ?
                       true : compare_accesses();
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

        if (pc == start_pc || (!runtime_inner && pc == exit_pc)) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, boundary, QEMU_PLUGIN_CB_RW_REGS_PC,
                (void *)(uintptr_t)pc);
        }
        if (!runtime_inner && pc >= start_pc && pc <= end_pc) {
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

        if (!strcmp(desc->name, "ar_audio_inner_control")) {
            tcg_control_register = desc->handle;
            g_free(reg);
            continue;
        }
        if (!strcmp(desc->name, "ar_audio_transform_control")) {
            tcg_transform_control_register = desc->handle;
            g_free(reg);
            continue;
        }
        reg->handle = desc->handle;
        reg->name = g_strdup(desc->name);
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
        write_report(runtime_inner);
    }
    fclose(report_file);
    g_mutex_unlock(&lock);
    g_ptr_array_free(registers, true);
    g_hash_table_destroy(footprint);
    g_hash_table_destroy(touched_bytes);
    g_ptr_array_free(pages, true);
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
        } else if (g_str_has_prefix(argv[i], "stable=")) {
            required_stable_calls = g_ascii_strtoull(argv[i] + 7, NULL, 0);
        } else if (!strcmp(argv[i], "candidate=inner")) {
            candidate_inner = true;
        } else if (!strcmp(argv[i], "candidate=tcg-inner")) {
            candidate_tcg = true;
        } else if (!strcmp(argv[i], "candidate=transform")) {
            candidate_transform = true;
        } else if (!strcmp(argv[i], "candidate=tcg-transform")) {
            candidate_tcg_transform = true;
        } else if (!strcmp(argv[i], "candidate=outer")) {
            candidate_outer = true;
        } else if (!strcmp(argv[i], "runtime=inner")) {
            runtime_inner = true;
        } else {
            fprintf(stderr, "unknown audio-shadow option: %s\n", argv[i]);
            return -1;
        }
    }
    if (!out_path || !*out_path || !start_pc || end_pc < start_pc || !exit_pc ||
        !required_stable_calls) {
        fprintf(stderr, "audio-shadow requires out=PATH and valid PCs\n");
        return -1;
    }
    if ((candidate_inner + candidate_tcg + candidate_transform +
         candidate_tcg_transform + candidate_outer + runtime_inner) > 1) {
        fprintf(stderr, "candidate and runtime modes are exclusive\n");
        return -1;
    }
    if ((candidate_inner || candidate_tcg || runtime_inner) &&
        (start_pc != INNER_START_PC || end_pc != INNER_END_PC ||
         exit_pc != INNER_EXIT_PC)) {
        fprintf(stderr, "inner acceleration requires the validated inner PCs\n");
        return -1;
    }
    if (candidate_transform &&
        (start_pc != TRANSFORM_START_PC || end_pc != TRANSFORM_END_PC ||
         exit_pc != TRANSFORM_EXIT_PC)) {
        fprintf(stderr, "transform candidate requires its validated PCs\n");
        return -1;
    }
    if (candidate_tcg_transform &&
        (start_pc != TRANSFORM_START_PC || end_pc != TRANSFORM_END_PC ||
         exit_pc != TRANSFORM_EXIT_PC)) {
        fprintf(stderr, "transform TCG candidate requires its validated PCs\n");
        return -1;
    }
    if (candidate_outer &&
        (start_pc != OUTER_START_PC || end_pc != OUTER_END_PC ||
         exit_pc != OUTER_EXIT_PC)) {
        fprintf(stderr, "outer candidate requires its validated PCs\n");
        return -1;
    }
    report_file = fopen(out_path, "w");
    if (!report_file) {
        perror("audio-shadow output");
        return -1;
    }
    registers = g_ptr_array_new_with_free_func(register_state_free);
    footprint = g_hash_table_new(g_int64_hash, g_int64_equal);
    touched_bytes = g_hash_table_new_full(g_int64_hash, g_int64_equal,
                                         g_free, NULL);
    pages = g_ptr_array_new_with_free_func(page_state_free);
    native_accesses = g_array_new(false, false, sizeof(Access));
    shadow_accesses = g_array_new(false, false, sizeof(Access));
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate, NULL);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
