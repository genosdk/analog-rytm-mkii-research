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
#include "system/reset.h"
#include "system/system.h"
#include "target/m68k/cpu.h"

#define AR_INTC0_BASE 0xFC048000u
#define AR_INTC1_BASE 0xFC04C000u
#define AR_INTC2_BASE 0xFC050000u
#define AR_PIT0_BASE  0xFC080000u
#define AR_PIT_STRIDE 0x00004000u
#define AR_DTIM0_BASE 0xFC070000u
#define AR_DTIM_STRIDE 0x00004000u
#define AR_UART8_BASE 0xEC070000u

#define AR_PIT_PCSR_EN   0x0001
#define AR_PIT_PCSR_RLD  0x0002
#define AR_PIT_PCSR_PIF  0x0004
#define AR_PIT_PCSR_PIE  0x0008
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

typedef struct ARDtimState {
    MemoryRegion iomem;
    uint16_t dtmr;
    uint8_t dtxmr;
    uint8_t dter;
    uint32_t dtrr;
    uint32_t dtcr;
    int64_t epoch_ns;
    uint32_t epoch_count;
    unsigned index;
} ARDtimState;

struct ARCoreState {
    M68kCPU *cpu;
    ARIntcState intc[3];
    ARPitState pit[4];
    ARDtimState dtim[4];
    DeviceState *uart8;
    qemu_irq uart8_irq;
    MemoryRegion uart8_proxy;
    bool uart8_boot_applied;
};

static ARCoreState *ar_core;

static unsigned ar_icr_level(uint8_t icr)
{
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
    case 0x1c:
        if (value & 0x40) {
            s->imr = ~0ULL;
        } else {
            s->imr |= 1ULL << (value & 0x3f);
        }
        break;
    case 0x1d:
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

/*
 * MAIN inherits DTIM0 as a free-running delay counter from the bootloader and
 * reads DTCN before it programs any DTIM registers itself. Model that inherited
 * state from QEMU's virtual clock; retain the remaining registers so later
 * firmware initialization observes its own writes.
 */
static uint32_t ar_dtim_counter(ARDtimState *s)
{
    int64_t elapsed = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->epoch_ns;
    uint64_t ticks = ((uint64_t)MAX(elapsed, (int64_t)0) * AR_PIT_BUS_HZ) /
                     1000000000ULL;
    return s->epoch_count + (uint32_t)ticks;
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

    switch (addr & 0x3fff) {
    case 0x00: s->dtmr = value; break;
    case 0x02: s->dtxmr = value; break;
    case 0x03: s->dter &= ~(uint8_t)value; break;
    case 0x04: s->dtrr = value; break;
    case 0x08: s->dtcr = value; break;
    case 0x0c:
        s->epoch_count = value;
        s->epoch_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        break;
    default: break;
    }
}

static const MemoryRegionOps ar_dtim_ops = {
    .read = ar_dtim_read,
    .write = ar_dtim_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void ar_dtim_init(MemoryRegion *sysmem, ARCoreState *c)
{
    unsigned i;

    for (i = 0; i < 4; i++) {
        ARDtimState *s = &c->dtim[i];
        s->index = i;
        s->epoch_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        memory_region_init_io(&s->iomem, NULL, &ar_dtim_ops, s,
                              "ar-mk2-dtim", 0x4000);
        memory_region_add_subregion_overlap(sysmem,
                                            AR_DTIM0_BASE + i * AR_DTIM_STRIDE,
                                            &s->iomem, 10);
    }
}

/* QEMU resets the mcf-uart after machine construction, while the physical
 * Rytm boot stage has already enabled UART8 before MAIN begins. Mark that
 * inherited state invalid on reset and apply it lazily on the first MAIN
 * register access, which necessarily occurs after QEMU's device reset. */
static void ar_uart8_mark_boot_unapplied(void *opaque)
{
    ARCoreState *c = opaque;
    c->uart8_boot_applied = false;
}

static void ar_uart8_apply_boot_state(ARCoreState *c)
{
    if (c->uart8_boot_applied) {
        return;
    }
    mcf_uart_write(c->uart8, 0x08, 0x01, 1);
    mcf_uart_write(c->uart8, 0x08, 0x04, 1);
    c->uart8_boot_applied = true;
}

static uint64_t ar_uart8_proxy_read(void *opaque, hwaddr addr, unsigned size)
{
    ARCoreState *c = opaque;
    ar_uart8_apply_boot_state(c);
    return mcf_uart_read(c->uart8, addr, size);
}

static void ar_uart8_proxy_write(void *opaque, hwaddr addr,
                                 uint64_t value, unsigned size)
{
    ARCoreState *c = opaque;
    ar_uart8_apply_boot_state(c);
    mcf_uart_write(c->uart8, addr, value, size);
}

static const MemoryRegionOps ar_uart8_proxy_ops = {
    .read = ar_uart8_proxy_read,
    .write = ar_uart8_proxy_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

/*
 * UART8 RX is routed to eDMA channel 34 on the Rytm. INTC1 source 26 is the
 * channel-completion interrupt, not the UART request level itself. Until the
 * eDMA request path is modeled, keep the UART request off the CPU INTC input;
 * directly wiring it there leaves unread FIFO data asserted and causes an
 * artificial vector-154 interrupt storm.
 */
static void ar_uart8_dma_request(void *opaque, int n, int level)
{
    /* Request is intentionally consumed by the future eDMA model. */
}

static void ar_uart8_init(MemoryRegion *sysmem, ARCoreState *c)
{
    MemoryRegion *mr;

    c->uart8_irq = qemu_allocate_irq(ar_uart8_dma_request, c, 34);
    c->uart8 = mcf_uart_create(c->uart8_irq, serial_hd(0));
    mr = sysbus_mmio_get_region(SYS_BUS_DEVICE(c->uart8), 0);
    memory_region_add_subregion_overlap(sysmem, AR_UART8_BASE, mr, 20);

    memory_region_init_io(&c->uart8_proxy, NULL, &ar_uart8_proxy_ops, c,
                          "ar-mk2-uart8-boot-proxy", 0x40);
    memory_region_add_subregion_overlap(sysmem, AR_UART8_BASE,
                                        &c->uart8_proxy, 30);
    qemu_register_reset(ar_uart8_mark_boot_unapplied, c);
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

    ar_dtim_init(sysmem, ar_core);
    ar_uart8_init(sysmem, ar_core);
}
