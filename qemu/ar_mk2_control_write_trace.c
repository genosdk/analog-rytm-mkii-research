/*
 * QEMU plugin for tracing stock-firmware writes to the AR MKII DSPI1 source
 * buffer.  Build against the matching QEMU tree's qemu-plugin.h.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

#define DEFAULT_START UINT64_C(0x800063c0)
#define DEFAULT_END   UINT64_C(0x80006797)

static FILE *trace_file;
static GMutex trace_lock;
static uint64_t trace_start = DEFAULT_START;
static uint64_t trace_end = DEFAULT_END;
static uint64_t sequence;
static struct qemu_plugin_register *pc_register;

static uint64_t live_pc(uint64_t translated_pc)
{
    g_autoptr(GByteArray) bytes = g_byte_array_new();
    uint64_t value = 0;

    if (!pc_register || !qemu_plugin_read_register(pc_register, bytes) ||
        bytes->len == 0 || bytes->len > sizeof(value)) {
        return translated_pc;
    }
    for (guint index = 0; index < bytes->len; index++) {
        value = (value << 8) | bytes->data[index];
    }
    return value;
}

static bool parse_u64_arg(const char *text, uint64_t *result)
{
    char *tail;
    unsigned long long value;

    errno = 0;
    value = strtoull(text, &tail, 0);
    if (errno || tail == text || *tail != '\0') {
        return false;
    }
    *result = (uint64_t)value;
    return true;
}

static uint64_t memory_value(qemu_plugin_mem_value value)
{
    switch (value.type) {
    case QEMU_PLUGIN_MEM_VALUE_U8:
        return value.data.u8;
    case QEMU_PLUGIN_MEM_VALUE_U16:
        return value.data.u16;
    case QEMU_PLUGIN_MEM_VALUE_U32:
        return value.data.u32;
    case QEMU_PLUGIN_MEM_VALUE_U64:
        return value.data.u64;
    case QEMU_PLUGIN_MEM_VALUE_U128:
        return value.data.u128.low;
    default:
        return 0;
    }
}

static void trace_write(unsigned int vcpu_index,
                        qemu_plugin_meminfo_t meminfo,
                        uint64_t vaddr, void *userdata)
{
    const uint64_t translated_pc = (uint64_t)(uintptr_t)userdata;
    struct qemu_plugin_hwaddr *hwaddr;
    qemu_plugin_mem_value value;
    uint64_t paddr;
    uint64_t final_addr;
    unsigned int size;

    hwaddr = qemu_plugin_get_hwaddr(meminfo, vaddr);
    if (!hwaddr || qemu_plugin_hwaddr_is_io(hwaddr)) {
        return;
    }

    paddr = qemu_plugin_hwaddr_phys_addr(hwaddr);
    size = 1U << qemu_plugin_mem_size_shift(meminfo);
    final_addr = paddr > UINT64_MAX - (size - 1) ? UINT64_MAX
                                                  : paddr + size - 1;
    if (final_addr < trace_start || paddr > trace_end) {
        return;
    }

    value = qemu_plugin_mem_get_value(meminfo);
    g_mutex_lock(&trace_lock);
    sequence++;
    fprintf(trace_file,
            "{\"sequence\":%" PRIu64
            ",\"vcpu\":%u,\"pc\":\"0x%08" PRIX64
            "\",\"translated_pc\":\"0x%08" PRIX64
            "\",\"vaddr\":\"0x%08" PRIX64
            "\",\"paddr\":\"0x%08" PRIX64
            "\",\"size\":%u,\"value\":\"0x%0*" PRIX64 "\"}\n",
            sequence, vcpu_index, live_pc(translated_pc), translated_pc,
            vaddr, paddr, size,
            size <= 8 ? (int)(size * 2) : 16, memory_value(value));
    fflush(trace_file);
    g_mutex_unlock(&trace_lock);
}

static void translate_block(struct qemu_plugin_tb *tb, void *userdata)
{
    size_t count = qemu_plugin_tb_n_insns(tb);

    (void)userdata;
    for (size_t index = 0; index < count; index++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, index);
        uint64_t pc = qemu_plugin_insn_vaddr(insn);

        qemu_plugin_register_vcpu_mem_cb(
            insn, trace_write, QEMU_PLUGIN_CB_R_REGS, QEMU_PLUGIN_MEM_W,
            (void *)(uintptr_t)pc);
    }
}

static void vcpu_init(unsigned int vcpu_index, void *userdata)
{
    g_autoptr(GArray) registers = qemu_plugin_get_registers();

    (void)vcpu_index;
    (void)userdata;
    for (guint index = 0; index < registers->len; index++) {
        qemu_plugin_reg_descriptor *descriptor = &g_array_index(
            registers, qemu_plugin_reg_descriptor, index);
        if (g_ascii_strcasecmp(descriptor->name, "pc") == 0) {
            pc_register = descriptor->handle;
            return;
        }
    }
}

static void plugin_exit(void *userdata)
{
    (void)userdata;
    g_mutex_lock(&trace_lock);
    if (trace_file) {
        fflush(trace_file);
        fclose(trace_file);
        trace_file = NULL;
    }
    g_mutex_unlock(&trace_lock);
    g_mutex_clear(&trace_lock);
}

QEMU_PLUGIN_EXPORT
int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info,
                        int argc, char **argv)
{
    const char *output_path = NULL;

    if (!info->system_emulation) {
        fprintf(stderr, "ar_mk2_control_write_trace requires system emulation\n");
        return -1;
    }

    for (int index = 0; index < argc; index++) {
        if (strncmp(argv[index], "out=", 4) == 0) {
            output_path = argv[index] + 4;
        } else if (strncmp(argv[index], "start=", 6) == 0) {
            if (!parse_u64_arg(argv[index] + 6, &trace_start)) {
                fprintf(stderr, "invalid start argument: %s\n", argv[index]);
                return -1;
            }
        } else if (strncmp(argv[index], "end=", 4) == 0) {
            if (!parse_u64_arg(argv[index] + 4, &trace_end)) {
                fprintf(stderr, "invalid end argument: %s\n", argv[index]);
                return -1;
            }
        } else {
            fprintf(stderr, "unknown plugin argument: %s\n", argv[index]);
            return -1;
        }
    }

    if (!output_path || !*output_path || trace_start > trace_end) {
        fprintf(stderr, "usage: -plugin file=PLUGIN,out=PATH[,start=N,end=N]\n");
        return -1;
    }
    trace_file = fopen(output_path, "w");
    if (!trace_file) {
        fprintf(stderr, "cannot open trace output %s: %s\n",
                output_path, strerror(errno));
        return -1;
    }

    g_mutex_init(&trace_lock);
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate_block, NULL);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
