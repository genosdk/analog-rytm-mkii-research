/*
 * MCF5441x DMA timer overlay for Analog Rytm MKII emulation.
 *
 * This supersedes the early counter-only DTIM backing in ar_mk2_intc_pit.c.
 * It keeps all four counters monotonic (matching bootloader-inherited timer
 * state) and implements reference-event scheduling plus INTC0 sources 32..35.
 */

#include "qemu/osdep.h"
#include "qemu/module.h"
#include "qemu/notify.h"
#include "qemu/timer.h"
#include "hw/core/boards.h"
#include "system/memory.h"
#include "system/physmem.h"
#include "system/system.h"
#include "qom/object.h"

#define AR_DTIM0_BASE      0xFC070000u
#define AR_DTIM_STRIDE     0x00004000u
#define AR_DTIM_SIZE       0x4000u
#define AR_INTC0_IFRH      0xFC048010u
#define AR_DTIM_BUS_HZ     125000000ULL

#define AR_DTMR_RST        0x0001u
#define AR_DTER_REF        0x02u

typedef struct ARDtimState {
    MemoryRegion iomem;
    QEMUTimer *ref_timer;
    uint16_t dtmr;
    uint8_t dtxmr;
    uint8_t dter;
    uint32_t dtrr;
    uint32_t dtcr;
    int64_t epoch_ns;
    uint32_t epoch_count;
    unsigned index;
} ARDtimState;

static ARDtimState *ar_dtim;

static uint64_t ar_dtim_divisor(const ARDtimState *s)
{
    return ((s->dtmr >> 8) & 0xffu) + 1u;
}

static uint32_t ar_dtim_counter_at(ARDtimState *s, int64_t now)
{
    int64_t elapsed = MAX(now - s->epoch_ns, (int64_t)0);
    uint64_t ticks = ((uint64_t)elapsed * AR_DTIM_BUS_HZ) /
                     (ar_dtim_divisor(s) * 1000000000ULL);
    return s->epoch_count + (uint32_t)ticks;
}

static uint32_t ar_dtim_counter(ARDtimState *s)
{
    return ar_dtim_counter_at(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

static void ar_dtim_set_irq(unsigned index, bool level)
{
    uint8_t raw[4];
    uint32_t ifr;

    physical_memory_read(AR_INTC0_IFRH, raw, sizeof(raw));
    ifr = ldl_be_p(raw);
    if (level) {
        ifr |= 1u << index;
    } else {
        ifr &= ~(1u << index);
    }
    stl_be_p(raw, ifr);
    physical_memory_write(AR_INTC0_IFRH, raw, sizeof(raw));
}

static int64_t ar_dtim_period_ns(const ARDtimState *s)
{
    uint64_t ticks = s->dtrr ? (uint64_t)s->dtrr : 1ULL;
    uint64_t ns = (ticks * ar_dtim_divisor(s) * 1000000000ULL) /
                  AR_DTIM_BUS_HZ;
    return MAX((int64_t)1000, (int64_t)ns);
}

static void ar_dtim_schedule(ARDtimState *s)
{
    if (!(s->dtmr & AR_DTMR_RST) || !s->dtrr) {
        timer_del(s->ref_timer);
        return;
    }
    timer_mod_ns(s->ref_timer,
                 qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ar_dtim_period_ns(s));
}

static void ar_dtim_fire(void *opaque)
{
    ARDtimState *s = opaque;

    s->dter |= AR_DTER_REF;
    ar_dtim_set_irq(s->index, true);

    /* Firmware stops one-shot users such as DTIM1 in the ISR by clearing
     * DTMR. Periodic users such as DTIM3 leave RST asserted, so rescheduling
     * here naturally supports both patterns. */
    if (s->dtmr & AR_DTMR_RST) {
        ar_dtim_schedule(s);
    }
}

static uint64_t ar_dtim_read(void *opaque, hwaddr addr, unsigned size)
{
    ARDtimState *s = opaque;
    switch (addr & 0x3fff) {
    case 0x00: return s->dtmr;
    case 0x02: return s->dtxmr;
    case 0x03: return s->dter;
    case 0x04: return s->dtrr;
    case 0x08: return s->dtcr;
    case 0x0c: return ar_dtim_counter(s);
    default: return 0;
    }
}

static void ar_dtim_write(void *opaque, hwaddr addr,
                          uint64_t value, unsigned size)
{
    ARDtimState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    switch (addr & 0x3fff) {
    case 0x00:
        s->epoch_count = ar_dtim_counter_at(s, now);
        s->epoch_ns = now;
        s->dtmr = value;
        if (!(s->dtmr & AR_DTMR_RST)) {
            timer_del(s->ref_timer);
            ar_dtim_set_irq(s->index, false);
        } else {
            ar_dtim_schedule(s);
        }
        break;
    case 0x02:
        s->dtxmr = value;
        break;
    case 0x03:
        s->dter &= ~(uint8_t)value;
        if (value & AR_DTER_REF) {
            ar_dtim_set_irq(s->index, false);
        }
        break;
    case 0x04:
        s->dtrr = value;
        if (s->dtmr & AR_DTMR_RST) {
            ar_dtim_schedule(s);
        }
        break;
    case 0x08:
        s->dtcr = value;
        break;
    case 0x0c:
        s->epoch_count = value;
        s->epoch_ns = now;
        break;
    default:
        break;
    }
}

static const MemoryRegionOps ar_dtim_ops = {
    .read = ar_dtim_read,
    .write = ar_dtim_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void ar_mk2_dtim_init(MemoryRegion *sysmem)
{
    unsigned i;

    if (ar_dtim) {
        return;
    }
    ar_dtim = g_new0(ARDtimState, 4);
    for (i = 0; i < 4; i++) {
        ARDtimState *s = &ar_dtim[i];
        s->index = i;
        s->epoch_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        s->ref_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ar_dtim_fire, s);
        memory_region_init_io(&s->iomem, OBJECT(current_machine), &ar_dtim_ops, s,
                              "ar-mk2-dtim-overlay", AR_DTIM_SIZE);
        memory_region_add_subregion_overlap(sysmem,
                                            AR_DTIM0_BASE + i * AR_DTIM_STRIDE,
                                            &s->iomem, 50);
    }
}

static void ar_dtim_machine_done(Notifier *notifier, void *opaque)
{
    const char *type;

    if (!current_machine) {
        return;
    }
    type = object_get_typename(OBJECT(current_machine));
    if (!type || !strstr(type, "elektron-ar-mk2")) {
        return;
    }
    ar_mk2_dtim_init(get_system_memory());
}

static Notifier ar_dtim_machine_done_notifier = {
    .notify = ar_dtim_machine_done,
};

static void ar_dtim_register(void)
{
    qemu_add_machine_init_done_notifier(&ar_dtim_machine_done_notifier);
}

type_init(ar_dtim_register)
