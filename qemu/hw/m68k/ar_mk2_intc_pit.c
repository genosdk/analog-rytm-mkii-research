/*
 * Minimal MCF5441x interrupt-controller, PIT and panel UART model used by the
 * Analog Rytm MKII research machine.
 *
 * This is intentionally narrow: it implements only the register behavior
 * required to deliver PIT0..PIT3 through the three MCF5441x INTC banks and
 * overlays QEMU's ColdFire UART model at the Rytm UART8 address.
 */

#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/timer.h"
#include "hw/core/irq.h"
#include "hw/core/sysbus.h"
#include "hw/m68k/mcf.h"
#include "system/memory.h"
#include "system/physmem.h"
#include "system/system.h"
#include "target/m68k/cpu.h"

#define AR_INTC0_BASE 0xFC048000u
#define AR_INTC1_BASE 0xFC04C000u
#define AR_INTC2_BASE 0xFC050000u
#define AR_PIT0_BASE  0xFC080000u
#define AR_PIT_STRIDE 0x00004000u
#define AR_UART8_BASE 0xEC070000u

#define AR_PIT_PCSR_EN   0x0001
#define AR_PIT_PCSR_RLD  0x0002
#define AR_PIT_PCSR_PIF  0x0004
#define AR_PIT_PCSR_PIE  0x0008
#define AR_PIT_PCSR_OVW  0x0010
#define AR_PIT_CLK_MASK  0x0F00

/* MCF5441x bus clock is board-dependent. 125 MHz gives the observed firmware
 * PIT modulus/prescaler values a plausible millisecond-scale cadence and can
 * be refined once hardware timing is measured. */
#define AR_PIT_BUS_HZ 125000000ULL

typedef struct ARCoreState ARCoreState;

typedef struct ARIntcState {
    MemoryRegion iomem;
    ARCoreState *core;
    uint64_t ipr;
    uint64_t imr;
    uint64_t ifr;
    uint64_t enabled;
    uint8_t icr[64];
    unsigned index;
} ARIntcState;

typedef struct ARPitState {
    MemoryRegion iomem;
    ARCoreState *core;
    QEMUTimer *timer;
    uint16_t pcsr;
    uint16_t pmr;
    int64_t deadline_ns;
    unsigned index;
} ARPitState;

struct ARCoreState {
    M68kCPU *cpu;
    ARIntcState intc[3];
    ARPitState pit[4];
    DeviceState *uart8;
    qemu_irq uart8_irq;
};

static ARCoreState *ar_core;

static unsigned ar_icr_level(uint8_t icr)
{
    /* OS 1.72 programs the currently observed sources with small values
     * (for example PIT0=1, PIT2=3). Preserve that behavior directly. */
    unsigned level = icr & 7;
    return level ? level : (icr ? 1 : 0);
}

static void ar_intc_update(ARCoreState *c)
{
    unsigned best_level = 0;
    int best_vector = 24;
    unsigned bank, source;

    for (bank = 0; bank < 3; bank++) {
        ARIntcState *s = &c->intc[bank];
        uint64_t active = (s->ipr | s->ifr) & s->enabled & ~s->imr;
        for (source = 0; source < 64; source++) {
            if (active & (1ULL << source)) {
                unsigned level = ar_icr_level(s->icr[source]);
                if (level >= best_level && level != 0) {
                    best_level = level;
                    best_vector = 64 + (bank * 64) + source;
                }
            }
        }
    }

    m68k_set_irq_level(c->cpu, best_level, best_vector);
}

static void ar_intc_set_irq(ARCoreState *c, unsigned bank,
                            unsigned source, bool level)
{
    ARIntcState *s;
    if (bank >= 3 || source >= 64) {
        return;
    }
    s = &c->intc[bank];
    if (level) {
        s->ipr |= 1ULL << source;
    } else {
        s->ipr &= ~(1ULL << source);
    }
    ar_intc_update(c);
}

static void ar_external_irq(void *opaque, int n, int level)
{
    ARCoreState *c = opaque;
    unsigned line = (unsigned)n;
    ar_intc_set_irq(c, line / 64, line % 64, level != 0);
}

static uint64_t ar_intc_read(void *opaque, hwaddr addr, unsigned size)
{
    ARIntcState *s = opaque;
    unsigned off = addr & 0xff;

    if (off >= 0x40 && off < 0x80) {
        return s->icr[off - 0x40];
    }
    switch (off) {
    case 0x00: return (uint32_t)(s->ipr >> 32);
    case 0x04: return (uint32_t)s->ipr;
    case 0x08: return (uint32_t)(s->imr >> 32);
    case 0x0c: return (uint32_t)s->imr;
    case 0x10: return (uint32_t)(s->ifr >> 32);
    case 0x14: return (uint32_t)s->ifr;
    case 0xe0:
        /* Software IACK value. The CPU already receives the full vector from
         * ar_intc_update(), but returning the bank-relative active vector is
         * useful for firmware that probes SWIACK. */
        {
            uint64_t active = (s->ipr | s->ifr) & s->enabled & ~s->imr;
            int best = -1;
            unsigned best_level = 0;
            unsigned i;
            for (i = 0; i < 64; i++) {
                if (active & (1ULL << i)) {
                    unsigned level = ar_icr_level(s->icr[i]);
                    if (level >= best_level && level != 0) {
                        best_level = level;
                        best = i;
                    }
                }
            }
            return best < 0 ? 24 : (64 + (s->index * 64) + best);
        }
    default:
        return 0;
    }
}

static void ar_intc_write(void *opaque, hwaddr addr,
                          uint64_t value, unsigned size)
{
    ARIntcState *s = opaque;
    unsigned off = addr & 0xff;

    if (off >= 0x40 && off < 0x80) {
        unsigned source = off - 0x40;
        s->icr[source] = value;
        if ((uint8_t)value) {
            s->enabled |= 1ULL << source;
        } else {
            s->enabled &= ~(1ULL << source);
        }
        ar_intc_update(s->core);
        return;
    }

    switch (off) {
    case 0x08:
        s->imr = (s->imr & 0xffffffffULL) | ((uint64_t)(uint32_t)value << 32);
        break;
    case 0x0c:
        s->imr = (s->imr & 0xffffffff00000000ULL) | (uint32_t)value;
        break;
    case 0x10:
        s->ifr = (s->ifr & 0xffffffffULL) | ((uint64_t)(uint32_t)value << 32);
        break;
    case 0x14:
        s->ifr = (s->ifr & 0xffffffff00000000ULL) | (uint32_t)value;
        break;
    case 0x1c: /* SIMR: set one mask bit; 0x40 means all. */
        if (value & 0x40) {
            s->imr = ~0ULL;
        } else {
            s->imr |= 1ULL << (value & 0x3f);
        }
        break;
    case 0x1d: /* CIMR: clear one mask bit; 0x40 means all. */
        if (value & 0x40) {
            s->imr = 0;
        } else {
            s->imr &= ~(1ULL << (value & 0x3f));
        }
        break;
    default:
        break;
    }
    ar_intc_update(s->core);
}

static const MemoryRegionOps ar_intc_ops = {
    .read = ar_intc_read,
    .write = ar_intc_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static uint64_t ar_pit_prescale(const ARPitState *s)
{
    return 1ULL << ((s->pcsr & AR_PIT_CLK_MASK) >> 8);
}

static int64_t ar_pit_period_ns(const ARPitState *s)
{
    uint64_t ticks = (uint64_t)s->pmr + 1;
    uint64_t prescale = ar_pit_prescale(s);
    uint64_t ns = (ticks * prescale * 1000000000ULL) / AR_PIT_BUS_HZ;
    if (ns < 1000) {
        ns = 1000;
    }
    return (int64_t)ns;
}

static void ar_pit_lower_irq(ARPitState *s)
{
    ar_intc_set_irq(s->core, 2, 13 + s->index, false);
}

static void ar_pit_schedule(ARPitState *s)
{
    if (!(s->pcsr & AR_PIT_PCSR_EN)) {
        timer_del(s->timer);
        s->deadline_ns = -1;
        return;
    }
    s->deadline_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ar_pit_period_ns(s);
    timer_mod_ns(s->timer, s->deadline_ns);
}

static void ar_pit_fire(void *opaque)
{
    ARPitState *s = opaque;
    s->pcsr |= AR_PIT_PCSR_PIF;
    if (s->pcsr & AR_PIT_PCSR_PIE) {
        ar_intc_set_irq(s->core, 2, 13 + s->index, true);
    }
    if ((s->pcsr & (AR_PIT_PCSR_EN | AR_PIT_PCSR_RLD)) ==
        (AR_PIT_PCSR_EN | AR_PIT_PCSR_RLD)) {
        ar_pit_schedule(s);
    } else {
        s->deadline_ns = -1;
    }
}

static uint16_t ar_pit_counter(ARPitState *s)
{
    int64_t now, remaining_ns;
    uint64_t ticks;
    if (!(s->pcsr & AR_PIT_PCSR_EN) || s->deadline_ns < 0) {
        return s->pmr;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    remaining_ns = MAX((int64_t)0, s->deadline_ns - now);
    ticks = ((uint64_t)remaining_ns * AR_PIT_BUS_HZ) /
            (ar_pit_prescale(s) * 1000000000ULL);
    return MIN(ticks, (uint64_t)0xffff);
}

static uint64_t ar_pit_read(void *opaque, hwaddr addr, unsigned size)
{
    ARPitState *s = opaque;
    switch (addr & 0x3fff) {
    case 0x00: return s->pcsr;
    case 0x02: return s->pmr;
    case 0x04: return ar_pit_counter(s);
    default: return 0;
    }
}

static void ar_pit_write(void *opaque, hwaddr addr,
                         uint64_t value, unsigned size)
{
    ARPitState *s = opaque;
    switch (addr & 0x3fff) {
    case 0x00:
        {
            uint16_t old_pif = s->pcsr & AR_PIT_PCSR_PIF;
            uint16_t v = value;
            if (v & AR_PIT_PCSR_PIF) {
                old_pif = 0;
                ar_pit_lower_irq(s);
            }
            s->pcsr = (v & ~AR_PIT_PCSR_PIF) | old_pif;
            ar_pit_schedule(s);
        }
        break;
    case 0x02:
        s->pmr = value;
        s->pcsr &= ~AR_PIT_PCSR_PIF;
        ar_pit_lower_irq(s);
        if (s->pcsr & AR_PIT_PCSR_EN) {
            ar_pit_schedule(s);
        }
        break;
    case 0x04:
        /* PCNTR is read-only on hardware. */
        break;
    default:
        break;
    }
}

static const MemoryRegionOps ar_pit_ops = {
    .read = ar_pit_read,
    .write = ar_pit_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 2,
};

static void ar_uart8_boot_enable(void)
{
    uint8_t cmd;

    /* MAIN assumes UART8 was already enabled by the preceding boot stage.
     * Recreate only that inherited state when launching a raw MAIN image. */
    cmd = 0x01; /* receiver enable */
    physical_memory_write(AR_UART8_BASE + 0x08, &cmd, sizeof(cmd));
    cmd = 0x04; /* transmitter enable */
    physical_memory_write(AR_UART8_BASE + 0x08, &cmd, sizeof(cmd));
}

static void ar_uart8_init(MemoryRegion *sysmem, ARCoreState *c)
{
    MemoryRegion *mr;

    /* UART8 is INTC1 source 26 on the MCF5441x. Use serial0 as its external
     * panel transport so -serial unix:... can connect the desktop bridge. */
    c->uart8_irq = qemu_allocate_irq(ar_external_irq, c, 64 + 26);
    c->uart8 = mcf_uart_create(c->uart8_irq, serial_hd(0));
    mr = sysbus_mmio_get_region(SYS_BUS_DEVICE(c->uart8), 0);
    memory_region_add_subregion_overlap(sysmem, AR_UART8_BASE, mr, 20);
    ar_uart8_boot_enable();
}

void ar_mk2_intc_pit_init(MemoryRegion *sysmem, M68kCPU *cpu)
{
    static const hwaddr intc_base[3] = {
        AR_INTC0_BASE, AR_INTC1_BASE, AR_INTC2_BASE,
    };
    unsigned i;

    ar_core = g_new0(ARCoreState, 1);
    ar_core->cpu = cpu;

    for (i = 0; i < 3; i++) {
        ARIntcState *s = &ar_core->intc[i];
        s->core = ar_core;
        s->index = i;
        s->imr = ~0ULL;
        memory_region_init_io(&s->iomem, NULL, &ar_intc_ops, s,
                              "ar-mk2-intc", 0x100);
        memory_region_add_subregion_overlap(sysmem, intc_base[i], &s->iomem, 10);
    }

    for (i = 0; i < 4; i++) {
        ARPitState *s = &ar_core->pit[i];
        s->core = ar_core;
        s->index = i;
        s->deadline_ns = -1;
        s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ar_pit_fire, s);
        memory_region_init_io(&s->iomem, NULL, &ar_pit_ops, s,
                              "ar-mk2-pit", 0x4000);
        memory_region_add_subregion_overlap(sysmem,
                                            AR_PIT0_BASE + i * AR_PIT_STRIDE,
                                            &s->iomem, 10);
    }

    ar_uart8_init(sysmem, ar_core);
}
