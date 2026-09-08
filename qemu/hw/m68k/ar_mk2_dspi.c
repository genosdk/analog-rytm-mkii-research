/*
 * Minimal MCF5441x DSPI0/DSPI1 model for Analog Rytm MKII emulation.
 *
 * OS 1.72 currently uses only the standard DSPI queue/status path we model:
 * MCR, TCR/CTAR backing, SR, RSER, PUSHR and POPR. Each PUSHR immediately
 * creates one zero-valued RX FIFO entry. This is sufficient to preserve the
 * firmware's transfer-completion and FIFO-count semantics while leaving the
 * attached SPI peripherals intentionally unmodeled.
 */

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/notify.h"
#include "hw/core/boards.h"
#include "system/memory.h"
#include "system/system.h"
#include "qom/object.h"

#define AR_DSPI0_BASE 0xFC05C000u
#define AR_DSPI1_BASE 0xFC03C000u
#define AR_DSPI_SIZE  0x4000u
#define AR_DSPI_FIFO_DEPTH 16

#define AR_DSPI_SR_EOQF   (1u << 28)
#define AR_DSPI_SR_TCF    (1u << 31)
#define AR_DSPI_SR_RFDF   (1u << 17)
#define AR_DSPI_SR_RXCTR_SHIFT 4
#define AR_DSPI_SR_RXCTR_MASK  (0xFu << AR_DSPI_SR_RXCTR_SHIFT)

typedef struct ARDspiState {
    MemoryRegion iomem;
    uint32_t mcr;
    uint32_t tcr;
    uint32_t ctar[8];
    uint32_t rser;
    uint32_t sr_flags;
    uint32_t rx_fifo[AR_DSPI_FIFO_DEPTH];
    unsigned rx_head;
    unsigned rx_count;
    unsigned index;
} ARDspiState;

static ARDspiState *ar_dspi;

static uint32_t ar_dspi_sr(const ARDspiState *s)
{
    uint32_t sr = s->sr_flags;
    unsigned count = MIN(s->rx_count, 15u);
    sr &= ~AR_DSPI_SR_RXCTR_MASK;
    sr |= count << AR_DSPI_SR_RXCTR_SHIFT;
    if (s->rx_count) {
        sr |= AR_DSPI_SR_RFDF;
    } else {
        sr &= ~AR_DSPI_SR_RFDF;
    }
    return sr;
}

static void ar_dspi_push_rx(ARDspiState *s, uint32_t value)
{
    unsigned tail;
    if (s->rx_count >= AR_DSPI_FIFO_DEPTH) {
        /* Keep the newest deterministic response instead of wedging startup. */
        s->rx_head = (s->rx_head + 1) % AR_DSPI_FIFO_DEPTH;
        s->rx_count--;
    }
    tail = (s->rx_head + s->rx_count) % AR_DSPI_FIFO_DEPTH;
    s->rx_fifo[tail] = value;
    s->rx_count++;
    s->sr_flags |= AR_DSPI_SR_EOQF | AR_DSPI_SR_TCF;
}

static uint32_t ar_dspi_pop_rx(ARDspiState *s)
{
    uint32_t value = 0;
    if (s->rx_count) {
        value = s->rx_fifo[s->rx_head];
        s->rx_head = (s->rx_head + 1) % AR_DSPI_FIFO_DEPTH;
        s->rx_count--;
    }
    return value;
}

static uint64_t ar_dspi_read(void *opaque, hwaddr addr, unsigned size)
{
    ARDspiState *s = opaque;
    unsigned off = addr & 0x3fff;

    switch (off) {
    case 0x00: return s->mcr;
    case 0x08: return s->tcr;
    case 0x0c: return s->ctar[0];
    case 0x10: return s->ctar[1];
    case 0x14: return s->ctar[2];
    case 0x18: return s->ctar[3];
    case 0x1c: return s->ctar[4];
    case 0x20: return s->ctar[5];
    case 0x24: return s->ctar[6];
    case 0x28: return s->ctar[7];
    case 0x2c: return ar_dspi_sr(s);
    case 0x30: return s->rser;
    case 0x38: return ar_dspi_pop_rx(s);
    default: return 0;
    }
}

static void ar_dspi_write(void *opaque, hwaddr addr,
                          uint64_t value, unsigned size)
{
    ARDspiState *s = opaque;
    unsigned off = addr & 0x3fff;
    uint32_t v = value;

    switch (off) {
    case 0x00: s->mcr = v; break;
    case 0x08: s->tcr = v; break;
    case 0x0c: s->ctar[0] = v; break;
    case 0x10: s->ctar[1] = v; break;
    case 0x14: s->ctar[2] = v; break;
    case 0x18: s->ctar[3] = v; break;
    case 0x1c: s->ctar[4] = v; break;
    case 0x20: s->ctar[5] = v; break;
    case 0x24: s->ctar[6] = v; break;
    case 0x28: s->ctar[7] = v; break;
    case 0x2c:
        /* DSPI SR completion/error flags are write-one-to-clear. FIFO count is
         * derived from the queue and cannot be directly overwritten. */
        s->sr_flags &= ~v;
        break;
    case 0x30:
        s->rser = v;
        break;
    case 0x34:
        /* Attached devices are not modeled yet. Return deterministic zero data
         * while preserving one-RX-word-per-transfer semantics. */
        ar_dspi_push_rx(s, 0);
        break;
    default:
        break;
    }
}

static const MemoryRegionOps ar_dspi_ops = {
    .read = ar_dspi_read,
    .write = ar_dspi_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void ar_mk2_dspi_init(MemoryRegion *sysmem)
{
    static const hwaddr bases[2] = { AR_DSPI0_BASE, AR_DSPI1_BASE };
    unsigned i;

    if (ar_dspi) {
        return;
    }
    ar_dspi = g_new0(ARDspiState, 2);
    for (i = 0; i < 2; i++) {
        ARDspiState *s = &ar_dspi[i];
        s->index = i;
        memory_region_init_io(&s->iomem, OBJECT(current_machine), &ar_dspi_ops, s,
                              i ? "ar-mk2-dspi1" : "ar-mk2-dspi0",
                              AR_DSPI_SIZE);
        memory_region_add_subregion_overlap(sysmem, bases[i], &s->iomem, 40);
    }
}

static void ar_dspi_machine_done(Notifier *notifier, void *opaque)
{
    const char *type;

    if (!current_machine) {
        return;
    }
    type = object_get_typename(OBJECT(current_machine));
    if (!type || !strstr(type, "elektron-ar-mk2")) {
        return;
    }
    ar_mk2_dspi_init(get_system_memory());
}

static Notifier ar_dspi_machine_done_notifier = {
    .notify = ar_dspi_machine_done,
};

static void ar_dspi_register(void)
{
    qemu_add_machine_init_done_notifier(&ar_dspi_machine_done_notifier);
}

type_init(ar_dspi_register)
