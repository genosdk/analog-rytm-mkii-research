/*
 * QEMU plugin for profiling a fixed number of stock AR MKII renderer calls.
 * Build outside this repository against the pinned QEMU plugin SDK; no
 * firmware data or guest memory is captured.
 */
#include <glib.h>
#include <inttypes.h>
#include <qemu-plugin.h>
#include <stdio.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef struct {
    uint64_t pc;
    uint64_t count;
    size_t insns;
} Block;

static GHashTable *blocks;
static struct qemu_plugin_scoreboard *active_scoreboard;
static qemu_plugin_u64 active_score;
static uint64_t start_pc = 0x4011b3ae;
static uint64_t stop_pc = 0x4011cf0a;
static uint64_t service_limit = 100;
static uint64_t services;
static uint64_t completed;
static uint64_t total_insns;
static bool reported;

static gint compare_blocks(gconstpointer a, gconstpointer b)
{
    const Block *ba = a;
    const Block *bb = b;
    uint64_t ia = ba->count * ba->insns;
    uint64_t ib = bb->count * bb->insns;

    return ia < ib ? 1 : ia > ib ? -1 : 0;
}

static void report(void)
{
    GList *values = g_hash_table_get_values(blocks);
    g_autoptr(GString) out = g_string_new(NULL);
    unsigned n = 0;

    values = g_list_sort(values, compare_blocks);
    g_string_append_printf(out,
                           "audio-window services=%" PRIu64
                           " completed=%" PRIu64 " counted=%" PRIu64
                           " instructions=%" PRIu64 "\n",
                           services, completed, service_limit, total_insns);
    for (GList *it = values; it && n < 100; it = it->next, n++) {
        Block *block = it->data;

        if (!block->count) {
            break;
        }
        g_string_append_printf(out,
                               "0x%08" PRIx64 ", %zu, %" PRIu64
                               ", %" PRIu64 "\n",
                               block->pc, block->insns, block->count,
                               block->count * block->insns);
    }
    qemu_plugin_outs(out->str);
    g_list_free(values);
    reported = true;
}

static void count_block(unsigned int cpu_index, void *userdata)
{
    Block *block = userdata;

    (void)cpu_index;
    block->count++;
    total_insns += block->insns;
}

static void boundary(unsigned int cpu_index, void *userdata)
{
    Block *block = userdata;
    uint64_t pc = block->pc;

    if (stop_pc && pc == stop_pc &&
        qemu_plugin_u64_get(active_score, cpu_index)) {
        qemu_plugin_u64_set(active_score, cpu_index, 0);
        completed++;
        if (completed == service_limit && !reported) {
            report();
        }
        return;
    }
    if (pc == start_pc) {
        services++;
        qemu_plugin_u64_set(active_score, cpu_index,
                            services <= service_limit);
        if (!stop_pc && services > service_limit && !reported) {
            report();
        }
        if (services <= service_limit) {
            block->count++;
            total_insns += block->insns;
        }
    }
}

static void translate(struct qemu_plugin_tb *tb, void *userdata)
{
    uint64_t pc = qemu_plugin_tb_vaddr(tb);
    Block *block = g_hash_table_lookup(blocks, &pc);

    (void)userdata;
    if (!block) {
        block = g_new0(Block, 1);
        block->pc = pc;
        block->insns = qemu_plugin_tb_n_insns(tb);
        g_hash_table_insert(blocks, &block->pc, block);
    }
    if (pc == start_pc || (stop_pc && pc == stop_pc)) {
        qemu_plugin_register_vcpu_tb_exec_cb(
            tb, boundary, QEMU_PLUGIN_CB_NO_REGS, block);
    } else {
        qemu_plugin_register_vcpu_tb_exec_cond_cb(
            tb, count_block, QEMU_PLUGIN_CB_NO_REGS, QEMU_PLUGIN_COND_NE,
            active_score, 0, block);
    }
}

static void at_exit(void *userdata)
{
    (void)userdata;
    if (!reported) {
        report();
    }
    g_hash_table_destroy(blocks);
    qemu_plugin_scoreboard_free(active_scoreboard);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    (void)info;
    for (int i = 0; i < argc; i++) {
        if (g_str_has_prefix(argv[i], "start=")) {
            start_pc = g_ascii_strtoull(argv[i] + 6, NULL, 0);
        } else if (g_str_has_prefix(argv[i], "stop=")) {
            stop_pc = g_ascii_strtoull(argv[i] + 5, NULL, 0);
        } else if (g_str_has_prefix(argv[i], "services=")) {
            service_limit = g_ascii_strtoull(argv[i] + 9, NULL, 0);
        } else {
            fprintf(stderr, "unknown audio-window option: %s\n", argv[i]);
            return -1;
        }
    }
    if (!start_pc || !service_limit) {
        return -1;
    }
    blocks = g_hash_table_new_full(g_int64_hash, g_int64_equal, NULL, g_free);
    active_scoreboard = qemu_plugin_scoreboard_new(sizeof(uint64_t));
    active_score = qemu_plugin_scoreboard_u64(active_scoreboard);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate, NULL);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
