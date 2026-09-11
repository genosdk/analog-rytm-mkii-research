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
#include "chardev/char.h"
#include "hw/core/irq.h"
#include "hw/core/sysbus.h"
#include "hw/m68k/mcf.h"
#include "system/memory.h"
#include "system/physmem.h"
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
#define AR_UART9_BASE 0xEC074000u
#define AR_EDMA_BASE  0xFC044000u
#define AR_EDMA_SIZE  0x00002000u
#define AR_EDMA_TCD_BASE 0x1000u
#define AR_EDMA_TCD_SIZE 0x20u
#define AR_EDMA_CHANNELS 64u
#define AR_EDMA_DSPI1_TX_CHANNEL 15u
#define AR_EDMA_SSI1_TX_CHANNEL 54u
#define AR_TYPE8_RECORD 0xF8u
#define AR_TYPE8_START_NS 5000000000LL
#define AR_TYPE8_PERIOD_NS 10000000LL
/* Stock CCR 0x00056F00: SSI_CLOCK / 4 bit clock, 16 I2S slots,
 * 32 clocks/slot.  The explicit 48 kHz stream rate therefore requires a
 * 98.304 MHz SSI_CLOCK and one 32-frame DMA block every 666667 ns. */
#define AR_SSI1_CLOCK_HZ 98304000LL
#define AR_SSI1_BIT_DIVISOR 4LL
#define AR_SSI1_FRAME_BITS (16LL * 32LL)
#define AR_AUDIO_BLOCK_FRAMES 32LL
#define AR_AUDIO_BLOCK_PERIOD_NS \
    ((AR_AUDIO_BLOCK_FRAMES * AR_SSI1_BIT_DIVISOR * \
      AR_SSI1_FRAME_BITS * NANOSECONDS_PER_SECOND + \
      AR_SSI1_CLOCK_HZ / 2) / AR_SSI1_CLOCK_HZ)
#define AR_AUDIO_TIMELINE_ADDR 0x8000FE54u
#define AR_AUDIO_TRIGGER_BLOCKS 8u
#define AR_AUDIO_TRIGGER_DELAY_TICKS 10u
#define AR_ACTIVE_PROFILE_ADDR 0x412FF99Fu
#define AR_PROFILE_STATE_BASE 0x413001DBu
#define AR_PROFILE_STATE_STRIDE 228u
#define AR_PROFILE_READY_OFFSET 88u
#define AR_PROFILE_COUNT 128u

#define AR_EDMA_TCD_SADDR 0x00
#define AR_EDMA_TCD_ATTR  0x04
#define AR_EDMA_TCD_SOFF  0x06
#define AR_EDMA_TCD_NBYTES 0x08
#define AR_EDMA_TCD_SLAST 0x0c
#define AR_EDMA_TCD_DADDR 0x10
#define AR_EDMA_TCD_CITER 0x14
#define AR_EDMA_TCD_DOFF  0x16
#define AR_EDMA_TCD_DLAST 0x18
#define AR_EDMA_TCD_BITER 0x1c
#define AR_EDMA_TCD_CSR   0x1e

#define AR_EDMA_CSR_INT_MAJOR 0x0002u
#define AR_EDMA_CSR_D_REQ     0x0008u
#define AR_EDMA_CSR_ESG       0x0010u
#define AR_EDMA_CSR_DONE      0x0080u
#define AR_EDMA_CSR_START     0x0001u

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

typedef struct AREdmaState {
    MemoryRegion iomem;
    ARCoreState *core;
    uint32_t cr;
    uint64_t erq;
    uint64_t intr;
    uint8_t dchpri[AR_EDMA_CHANNELS];
    uint8_t tcd[AR_EDMA_CHANNELS][AR_EDMA_TCD_SIZE];
    bool uart_busy;
} AREdmaState;

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
    AREdmaState edma;
    DeviceState *uart8;
    Chardev *uart8_chr;
    qemu_irq uart8_irq;
    MemoryRegion uart8_proxy;
    bool uart8_boot_applied;
    DeviceState *uart9;
    Chardev *uart9_chr;
    qemu_irq uart9_irq;
    QEMUTimer *type8_timer;
    QEMUTimer *audio_service_timer;
    uint64_t type8_feed_count;
    uint32_t type8_last_timeline;
    bool type8_timeline_seen;
    bool type8_dma_seen;
    bool type8_bootstrap_done;
    bool mock_audio_service;
    bool trigger_audio_service;
    unsigned audio_service_budget;
    unsigned audio_service_delay;
    bool audio_service_pending;
    bool audio_service_entered;
    unsigned audio_service_completed;
    uint8_t panel_pending_command;
    uint8_t panel_button_groups[16];
    bool audio_pad_held;
    bool audio_service_seen;
    bool source44_seen;
    bool source57_seen;
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
        if (s->index == 0 && (value & (1u << 12)) && !s->core->source44_seen) {
            s->core->source44_seen = true;
            qemu_log_mask(LOG_UNIMP,
                          "AR-MK2 TYPE8: firmware forced INTC0 source 44 "
                          "(vector 108)\n");
        }
        if (s->index == 0 && (value & (1u << 25)) && !s->core->source57_seen) {
            s->core->source57_seen = true;
            qemu_log_mask(LOG_UNIMP,
                          "AR-MK2 TYPE8: firmware forced INTC0 source 57 "
                          "(vector 121)\n");
        }
        if (s->index == 1 && (value & (1u << 31)) &&
            s->core->mock_audio_service) {
            s->core->audio_service_pending = true;
        }
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
 * Minimal MCF54418 eDMA engine.  The panel link uses channel 34 for UART8 RX
 * and channel 35 for UART8 TX.  TCD storage is byte-accurate so the firmware
 * observes its own register programming; requests execute one minor loop and
 * honor address offsets, modulo addressing, major-loop reload, D_REQ and
 * INT_MAJOR.
 */
static uint64_t ar_edma_load(const uint8_t *p, unsigned size)
{
    uint64_t value = 0;
    unsigned i;

    for (i = 0; i < size; i++) {
        value = (value << 8) | p[i];
    }
    return value;
}

static void ar_edma_store(uint8_t *p, uint64_t value, unsigned size)
{
    unsigned i;

    for (i = 0; i < size; i++) {
        unsigned shift = 8 * (size - 1 - i);
        p[i] = value >> shift;
    }
}

static uint32_t ar_edma_advance(uint32_t address, int16_t offset,
                                unsigned modulo)
{
    uint32_t next = address + offset;

    if (modulo && modulo < 32) {
        uint32_t mask = (1u << modulo) - 1;
        next = (address & ~mask) | (next & mask);
    }
    return next;
}

static unsigned ar_edma_iterations(uint16_t word)
{
    return word & ((word & 0x8000u) ? 0x01ffu : 0x7fffu);
}

static uint16_t ar_edma_set_iterations(uint16_t word, unsigned iterations)
{
    uint16_t mask = (word & 0x8000u) ? 0x01ffu : 0x7fffu;

    return (word & ~mask) | (iterations & mask);
}

static void ar_edma_set_irq(AREdmaState *s, unsigned channel, bool level)
{
    if (channel >= 56 && channel <= 63) {
        /* MCF5441x groups eDMA56..63 onto INTC2 source 0. OS 1.72 installs
         * the channel-59 completion ISR at vector 192. */
        ar_intc_set_irq(s->core, 2, 0, level);
    } else if (channel >= 32 && channel <= 55) {
        ar_intc_set_irq(s->core, 1, channel - 8, level);
    }
}

static void ar_edma_complete(AREdmaState *s, unsigned channel)
{
    uint8_t *tcd = s->tcd[channel];
    uint16_t csr = lduw_be_p(tcd + AR_EDMA_TCD_CSR);

    csr |= AR_EDMA_CSR_DONE;
    stw_be_p(tcd + AR_EDMA_TCD_CSR, csr);
    if (csr & AR_EDMA_CSR_D_REQ) {
        s->erq &= ~(1ULL << channel);
    }
    if (csr & AR_EDMA_CSR_INT_MAJOR) {
        s->intr |= 1ULL << channel;
        ar_edma_set_irq(s, channel, true);
    }
    qemu_log_mask(LOG_UNIMP,
                  "AR-MK2 eDMA complete channel=%u saddr=%08x daddr=%08x\n",
                  channel, ldl_be_p(tcd + AR_EDMA_TCD_SADDR),
                  ldl_be_p(tcd + AR_EDMA_TCD_DADDR));
}

static void ar_panel_audio_observe(ARCoreState *c, uint8_t value)
{
    uint8_t command = c->panel_pending_command;

    if (command) {
        c->panel_pending_command = 0;
        if ((command & 0xf0) == 0x20) {
            unsigned group = command & 0x0f;
            uint8_t falling = c->panel_button_groups[group] & ~value;
            uint8_t rising = value & ~c->panel_button_groups[group];
            bool was_held = c->audio_pad_held;

            c->panel_button_groups[group] = value;
            c->audio_pad_held = c->panel_button_groups[2] ||
                                c->panel_button_groups[3];
            if (c->trigger_audio_service && (group == 2 || group == 3) &&
                rising) {
                c->audio_service_budget = AR_AUDIO_TRIGGER_BLOCKS;
                c->audio_service_delay = AR_AUDIO_TRIGGER_DELAY_TICKS;
                qemu_log_mask(LOG_UNIMP,
                              "AR-MK2 AUDIO: pad edge group=%u mask=%02x; "
                              "scheduled %u renderer blocks\n",
                              group, rising, AR_AUDIO_TRIGGER_BLOCKS);
            }
            if (c->trigger_audio_service && (group == 2 || group == 3) &&
                falling && was_held && !c->audio_pad_held) {
                unsigned in_flight = c->audio_service_pending ||
                                     c->audio_service_entered;

                c->audio_service_budget = AR_AUDIO_TRIGGER_BLOCKS - in_flight;
                qemu_log_mask(LOG_UNIMP,
                              "AR-MK2 AUDIO: final pad release group=%u "
                              "mask=%02x completed=%u; bounded %u-block "
                              "release tail\n",
                              group, falling, c->audio_service_completed,
                              AR_AUDIO_TRIGGER_BLOCKS);
            }
        }
        return;
    }
    if ((value & 0xf0) == 0x20 || (value & 0xf0) == 0x30) {
        c->panel_pending_command = value;
    }
}

static bool ar_edma_service(AREdmaState *s, unsigned channel)
{
    uint8_t *tcd;
    g_autofree uint8_t *buffer = NULL;
    uint32_t saddr, daddr, nbytes;
    uint16_t attr, citer_word, csr;
    unsigned citer;
    int16_t soff, doff;
    unsigned smod, dmod, ssize, dsize, pos;

    if (channel >= AR_EDMA_CHANNELS ||
        !(s->erq & (1ULL << channel))) {
        return false;
    }

    tcd = s->tcd[channel];
    citer_word = lduw_be_p(tcd + AR_EDMA_TCD_CITER);
    citer = ar_edma_iterations(citer_word);
    nbytes = ldl_be_p(tcd + AR_EDMA_TCD_NBYTES) & 0x3fffffff;
    if (!citer || !nbytes || nbytes > 65536) {
        return false;
    }
    buffer = g_malloc(nbytes);

    saddr = ldl_be_p(tcd + AR_EDMA_TCD_SADDR);
    daddr = ldl_be_p(tcd + AR_EDMA_TCD_DADDR);
    attr = lduw_be_p(tcd + AR_EDMA_TCD_ATTR);
    soff = (int16_t)lduw_be_p(tcd + AR_EDMA_TCD_SOFF);
    doff = (int16_t)lduw_be_p(tcd + AR_EDMA_TCD_DOFF);
    smod = (attr >> 11) & 0x1f;
    dmod = (attr >> 3) & 0x1f;

    /* Transfer one source and destination element at a time. SOFF/DOFF apply
     * after each element; zero naturally models a fixed FIFO register. */
    ssize = 1u << ((attr >> 8) & 0x7);
    for (pos = 0; pos < nbytes; pos += ssize) {
        physical_memory_read(saddr, buffer + pos,
                             MIN(ssize, nbytes - pos));
        saddr = ar_edma_advance(saddr, soff, smod);
    }
    if (channel == 34) {
        for (pos = 0; pos < nbytes; pos++) {
            ar_panel_audio_observe(s->core, buffer[pos]);
        }
    }
    if (channel == 36 && buffer[0] == AR_TYPE8_RECORD &&
        !s->core->type8_dma_seen) {
        s->core->type8_dma_seen = true;
        qemu_log_mask(LOG_UNIMP,
                      "AR-MK2 TYPE8: eDMA36 transferred 0xF8 to 0x%08x "
                      "nbytes=%u citer=%u biter=%u uart_isr=%02x\n",
                      daddr, nbytes, citer,
                      ar_edma_iterations(lduw_be_p(tcd + AR_EDMA_TCD_BITER)),
                      (unsigned)mcf_uart_read(s->core->uart9, 0x14, 1));
    }
    dsize = 1u << (attr & 0x7);
    for (pos = 0; pos < nbytes; pos += dsize) {
        physical_memory_write(daddr, buffer + pos,
                              MIN(dsize, nbytes - pos));
        daddr = ar_edma_advance(daddr, doff, dmod);
    }
    stl_be_p(tcd + AR_EDMA_TCD_SADDR, saddr);
    stl_be_p(tcd + AR_EDMA_TCD_DADDR, daddr);

    citer--;
    if (citer) {
        stw_be_p(tcd + AR_EDMA_TCD_CITER,
                 ar_edma_set_iterations(citer_word, citer));
        return true;
    }

    saddr += (int32_t)ldl_be_p(tcd + AR_EDMA_TCD_SLAST);
    stl_be_p(tcd + AR_EDMA_TCD_SADDR, saddr);
    csr = lduw_be_p(tcd + AR_EDMA_TCD_CSR);
    if (csr & AR_EDMA_CSR_ESG) {
        uint32_t scatter_gather = ldl_be_p(tcd + AR_EDMA_TCD_DLAST);

        if (csr & AR_EDMA_CSR_D_REQ) {
            s->erq &= ~(1ULL << channel);
        }
        if (csr & AR_EDMA_CSR_INT_MAJOR) {
            s->intr |= 1ULL << channel;
            ar_edma_set_irq(s, channel, true);
        }
        physical_memory_read(scatter_gather, tcd, AR_EDMA_TCD_SIZE);
        qemu_log_mask(LOG_UNIMP,
                      "AR-MK2 eDMA scatter/gather channel=%u next=%08x\n",
                      channel, scatter_gather);
        return true;
    }

    daddr += (int32_t)ldl_be_p(tcd + AR_EDMA_TCD_DLAST);
    stl_be_p(tcd + AR_EDMA_TCD_DADDR, daddr);
    stw_be_p(tcd + AR_EDMA_TCD_CITER,
             lduw_be_p(tcd + AR_EDMA_TCD_BITER));
    ar_edma_complete(s, channel);
    return true;
}

static void ar_edma_pump_channel(AREdmaState *s, unsigned channel)
{
    unsigned guard = 0;
    uint8_t *tcd = s->tcd[channel];
    uint16_t csr = lduw_be_p(tcd + AR_EDMA_TCD_CSR);

    /* A fresh peripheral request activates a reloaded major loop and clears
     * the prior DONE status.  In particular, OS 1.72 reuses DSPI1 channel 15
     * by writing CERQ, the new SADDR, then SERQ; it does not issue CDNE. */
    if ((s->erq & (1ULL << channel)) && (csr & AR_EDMA_CSR_DONE)) {
        stw_be_p(tcd + AR_EDMA_TCD_CSR, csr & ~AR_EDMA_CSR_DONE);
    }

    while (guard++ < 4096 && (s->erq & (1ULL << channel))) {
        csr = lduw_be_p(tcd + AR_EDMA_TCD_CSR);

        if (csr & AR_EDMA_CSR_DONE) {
            break;
        }
        if (!ar_edma_service(s, channel)) {
            break;
        }
    }
}

static void ar_edma_pump_dspi1_tx(AREdmaState *s)
{
    uint8_t *tcd = s->tcd[AR_EDMA_DSPI1_TX_CHANNEL];
    uint16_t csr;

    if (!(s->erq & (1ULL << AR_EDMA_DSPI1_TX_CHANNEL))) {
        return;
    }

    /* DSPI1 immediately consumes its transmit FIFO.  With D_REQ clear the
     * empty FIFO can request another major loop after DONE is asserted. */
    csr = lduw_be_p(tcd + AR_EDMA_TCD_CSR);
    stw_be_p(tcd + AR_EDMA_TCD_CSR, csr & ~AR_EDMA_CSR_DONE);
    ar_edma_pump_channel(s, AR_EDMA_DSPI1_TX_CHANNEL);
}

static void ar_edma_software_start(AREdmaState *s, unsigned channel)
{
    uint8_t *tcd = s->tcd[channel];
    uint16_t csr = lduw_be_p(tcd + AR_EDMA_TCD_CSR);
    uint16_t citer = lduw_be_p(tcd + AR_EDMA_TCD_CITER);
    bool self_link = (citer & 0x8000u) &&
                     (((citer >> 9) & 0x3fu) == channel);
    bool request_was_enabled = s->erq & (1ULL << channel);
    unsigned guard = 0;

    if (!(csr & AR_EDMA_CSR_START)) {
        return;
    }

    stw_be_p(tcd + AR_EDMA_TCD_CSR, csr & ~AR_EDMA_CSR_START);
    s->erq |= 1ULL << channel;
    do {
        if (!ar_edma_service(s, channel)) {
            break;
        }
        csr = lduw_be_p(tcd + AR_EDMA_TCD_CSR);
    } while (self_link && !(csr & AR_EDMA_CSR_DONE) && guard++ < 511);

    if (!request_was_enabled) {
        s->erq &= ~(1ULL << channel);
    }
}

static void ar_edma_pump_one_uart(AREdmaState *s, DeviceState *uart,
                                  unsigned rx_channel, unsigned tx_channel)
{
    unsigned guard = 0;

    while (guard++ < 64) {
        uint8_t isr = mcf_uart_read(uart, 0x14, 1);
        bool progress = false;

        if ((isr & 0x02) && (s->erq & (1ULL << rx_channel))) {
            progress |= ar_edma_service(s, rx_channel);
        }
        if ((isr & 0x01) && (s->erq & (1ULL << tx_channel))) {
            progress |= ar_edma_service(s, tx_channel);
        }
        if (!progress) {
            break;
        }
    }
}

static void ar_edma_pump_uarts(ARCoreState *c)
{
    AREdmaState *s = &c->edma;

    if (s->uart_busy) {
        return;
    }

    s->uart_busy = true;
    if (c->uart8) {
        ar_edma_pump_one_uart(s, c->uart8, 34, 35);
    }
    if (c->uart9) {
        ar_edma_pump_one_uart(s, c->uart9, 36, 37);
    }
    s->uart_busy = false;
}

static uint64_t ar_edma_read(void *opaque, hwaddr addr, unsigned size)
{
    AREdmaState *s = opaque;
    unsigned off = addr & (AR_EDMA_SIZE - 1);

    if (off >= AR_EDMA_TCD_BASE &&
        off + size <= AR_EDMA_TCD_BASE +
                      AR_EDMA_CHANNELS * AR_EDMA_TCD_SIZE) {
        unsigned rel = off - AR_EDMA_TCD_BASE;
        return ar_edma_load(&s->tcd[rel / AR_EDMA_TCD_SIZE]
                                  [rel % AR_EDMA_TCD_SIZE], size);
    }
    if (off >= 0x100 && off < 0x140 && size == 1) {
        return s->dchpri[off - 0x100];
    }

    switch (off) {
    case 0x00: return s->cr;
    case 0x08: return (uint32_t)(s->erq >> 32);
    case 0x0c: return (uint32_t)s->erq;
    case 0x20: return (uint32_t)(s->intr >> 32);
    case 0x24: return (uint32_t)s->intr;
    case 0x30:
    case 0x34:
        return 0;
    default:
        return 0;
    }
}

static void ar_edma_write(void *opaque, hwaddr addr,
                          uint64_t value, unsigned size)
{
    AREdmaState *s = opaque;
    unsigned off = addr & (AR_EDMA_SIZE - 1);
    unsigned channel;

    if (off >= AR_EDMA_TCD_BASE &&
        off + size <= AR_EDMA_TCD_BASE +
                      AR_EDMA_CHANNELS * AR_EDMA_TCD_SIZE) {
        unsigned rel = off - AR_EDMA_TCD_BASE;
        channel = rel / AR_EDMA_TCD_SIZE;
        ar_edma_store(&s->tcd[channel][rel % AR_EDMA_TCD_SIZE], value, size);
        if ((rel % AR_EDMA_TCD_SIZE) <= AR_EDMA_TCD_CSR &&
            (rel % AR_EDMA_TCD_SIZE) + size > AR_EDMA_TCD_CSR) {
            ar_edma_software_start(s, channel);
        }
        ar_edma_pump_uarts(s->core);
        return;
    }
    if (off >= 0x100 && off < 0x140 && size == 1) {
        s->dchpri[off - 0x100] = value;
        return;
    }

    switch (off) {
    case 0x00:
        s->cr = value;
        break;
    case 0x08:
        s->erq = (s->erq & 0xffffffffULL) |
                 ((uint64_t)(uint32_t)value << 32);
        break;
    case 0x0c:
        s->erq = (s->erq & 0xffffffff00000000ULL) |
                 (uint32_t)value;
        break;
    case 0x18:
        if (!(value & 0x80)) {
            if (value & 0x40) {
                s->erq = ~0ULL;
            } else {
                s->erq |= 1ULL << (value & 0x3f);
            }
            if ((value & 0x3f) == AR_EDMA_DSPI1_TX_CHANNEL) {
                ar_edma_pump_dspi1_tx(s);
            }
            ar_edma_pump_uarts(s->core);
        }
        break;
    case 0x19:
        if (!(value & 0x80)) {
            if (value & 0x40) {
                s->erq = 0;
            } else {
                s->erq &= ~(1ULL << (value & 0x3f));
            }
        }
        break;
    case 0x1c:
        if (!(value & 0x80)) {
            if (value & 0x40) {
                for (channel = 0; channel < AR_EDMA_CHANNELS; channel++) {
                    ar_edma_set_irq(s, channel, false);
                }
                s->intr = 0;
            } else {
                channel = value & 0x3f;
                s->intr &= ~(1ULL << channel);
                ar_edma_set_irq(s, channel, false);
            }
        }
        break;
    case 0x1e:
        if (!(value & 0x80)) {
            if (value & 0x40) {
                for (channel = 0; channel < AR_EDMA_CHANNELS; channel++) {
                    ar_edma_service(s, channel);
                }
            } else {
                ar_edma_service(s, value & 0x3f);
            }
        }
        break;
    case 0x1f:
        if (!(value & 0x80)) {
            if (value & 0x40) {
                for (channel = 0; channel < AR_EDMA_CHANNELS; channel++) {
                    uint16_t csr =
                        lduw_be_p(s->tcd[channel] + AR_EDMA_TCD_CSR);
                    stw_be_p(s->tcd[channel] + AR_EDMA_TCD_CSR,
                             csr & ~AR_EDMA_CSR_DONE);
                }
            } else {
                channel = value & 0x3f;
                uint16_t csr =
                    lduw_be_p(s->tcd[channel] + AR_EDMA_TCD_CSR);
                stw_be_p(s->tcd[channel] + AR_EDMA_TCD_CSR,
                         csr & ~AR_EDMA_CSR_DONE);
            }
        }
        break;
    default:
        break;
    }
}

static const MemoryRegionOps ar_edma_ops = {
    .read = ar_edma_read,
    .write = ar_edma_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void ar_edma_init(MemoryRegion *sysmem, ARCoreState *c)
{
    AREdmaState *s = &c->edma;

    s->core = c;
    memory_region_init_io(&s->iomem, NULL, &ar_edma_ops, s,
                          "ar-mk2-edma", AR_EDMA_SIZE);
    memory_region_add_subregion_overlap(sysmem, AR_EDMA_BASE, &s->iomem, 20);
}

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
    ARCoreState *c = opaque;

    ar_edma_pump_uarts(c);
}

/*
 * The standalone MAIN image has neither the persistent project loader nor the
 * external audio-clock device that normally completes this state.  Supply the
 * smallest non-proprietary bootstrap: one boolean readiness word for the
 * firmware-selected profile and the audio-timeline interrupt masks.  The
 * optional audio-service clock is gated separately and remains disabled by
 * default.  Wait for the firmware to install the relevant ICRs so this cannot
 * bypass initialization.
 */
static bool ar_type8_bootstrap(ARCoreState *c)
{
    uint8_t raw[4];
    uint32_t profile;
    uint32_t ready;
    hwaddr ready_addr;

    if (c->type8_bootstrap_done) {
        return true;
    }
    if (!c->intc[0].icr[44] || !c->intc[0].icr[57]) {
        return false;
    }

    physical_memory_read(AR_ACTIVE_PROFILE_ADDR, raw, sizeof(raw));
    profile = ldl_be_p(raw);
    if (profile >= AR_PROFILE_COUNT) {
        qemu_log_mask(LOG_GUEST_ERROR,
                      "AR-MK2 TYPE8: invalid active profile %u\n", profile);
        return false;
    }

    ready_addr = AR_PROFILE_STATE_BASE +
                 (profile * AR_PROFILE_STATE_STRIDE) +
                 AR_PROFILE_READY_OFFSET;
    physical_memory_read(ready_addr, raw, sizeof(raw));
    ready = ldl_be_p(raw);
    if (!ready) {
        stl_be_p(raw, 1);
        physical_memory_write(ready_addr, raw, sizeof(raw));
    }

    c->intc[0].imr &= ~((1ULL << 44) | (1ULL << 57));
    if ((c->mock_audio_service || c->trigger_audio_service) &&
        c->intc[1].icr[63]) {
        c->intc[1].imr &= ~(1ULL << 63);
    }
    ar_intc_update(c);
    c->type8_bootstrap_done = true;
    qemu_log_mask(LOG_UNIMP,
                  "AR-MK2 TYPE8: bootstrapped profile %u and unmasked "
                  "vectors 108/121%s\n", profile,
                  (c->mock_audio_service || c->trigger_audio_service) ?
                  "/191" : "");
    return true;
}

static void ar_audio_service_tick(ARCoreState *c, bool continuous)
{
    bool enabled = continuous ? c->mock_audio_service :
                                c->trigger_audio_service;

    if (!enabled || !c->type8_bootstrap_done) {
        return;
    }
    if (c->audio_service_pending &&
        !(c->intc[1].ifr & (1ULL << 63))) {
        c->audio_service_pending = false;
        c->audio_service_entered = true;
        qemu_log_mask(LOG_UNIMP,
                      "AR-MK2 AUDIO: entered vector 191 service\n");
    }
    if (c->audio_service_entered &&
        ((c->cpu->env.sr & SR_I) >> SR_I_SHIFT) < 5) {
        c->audio_service_entered = false;
        c->audio_service_delay = c->audio_pad_held ? 0 :
                                 AR_AUDIO_TRIGGER_DELAY_TICKS;
        c->audio_service_completed++;
        qemu_log_mask(LOG_UNIMP,
                      "AR-MK2 AUDIO: completed vector 191 service count=%u\n",
                      c->audio_service_completed);
        if (!continuous && c->audio_pad_held &&
            !c->audio_service_budget) {
            /* Keep one request in reserve only while a real panel bitmap bit
             * remains asserted. Key auto-repeat has no rising edge, and the
             * last release replaces this with the fixed release-tail budget. */
            c->audio_service_budget = 1;
        }
    }
    if (!continuous && c->audio_service_delay) {
        c->audio_service_delay--;
    }
    if (continuous) {
        /* SSI1 TX FIFO demand drives eDMA54.  Its stock completion ISR at
         * 0x40118AF2 acknowledges channel 54 and software-forces source 63. */
        if (!c->audio_service_pending && !c->audio_service_entered &&
            !(c->edma.intr & (1ULL << AR_EDMA_SSI1_TX_CHANNEL))) {
            ar_edma_pump_channel(&c->edma, AR_EDMA_SSI1_TX_CHANNEL);
        }
        return;
    }
    if (!c->audio_service_pending && !c->audio_service_entered &&
        c->audio_service_budget && !c->audio_service_delay &&
        c->intc[1].icr[63]) {
        ar_edma_pump_dspi1_tx(&c->edma);
        ar_edma_pump_channel(&c->edma, 42);
        ar_edma_pump_channel(&c->edma, 30);
        c->intc[1].ifr |= 1ULL << 63;
        ar_intc_update(c);
        c->audio_service_pending = true;
        if (c->audio_service_budget) {
            c->audio_service_budget--;
        }
        if (!c->audio_service_seen) {
            c->audio_service_seen = true;
            qemu_log_mask(LOG_UNIMP,
                          "AR-MK2 AUDIO: forced INTC1 source 63 "
                          "(vector 191)\n");
        }
    }
}

static void ar_audio_service_feed(void *opaque)
{
    ARCoreState *c = opaque;

    ar_audio_service_tick(c, true);
    if (!c->mock_audio_service && c->trigger_audio_service &&
        c->audio_pad_held) {
        /* A held pad may use the SSI-derived 32-frame cadence. The pending /
         * entered lifecycle still prevents interrupt coalescing; release-tail
         * work remains on the slower Type-8 scheduler for UI headroom. */
        ar_audio_service_tick(c, false);
    }
    timer_mod_ns(c->audio_service_timer,
                 qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                 AR_AUDIO_BLOCK_PERIOD_NS);
}

/*
 * UART9 is the firmware's stream/control input: eDMA channel 36 passes each
 * received byte to callback 0x4007E648 and its byte-stream parser.  F8 is a
 * complete one-byte asynchronous type-8 record there.  Callback 0x400805AC
 * samples DTIM0 and starts the source-44/source-57 audio timeline interrupt
 * chain.  No firmware bytes or proprietary payload are synthesized here: the
 * type-8 record has no body.
 */
static void ar_type8_feed(void *opaque)
{
    ARCoreState *c = opaque;
    const uint8_t record = AR_TYPE8_RECORD;
    uint8_t raw[4];
    uint32_t timeline;

    if (!ar_type8_bootstrap(c)) {
        timer_mod_ns(c->type8_timer,
                     qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                     AR_TYPE8_PERIOD_NS);
        return;
    }

    if (c->uart9_chr && qemu_chr_be_can_write(c->uart9_chr) > 0) {
        qemu_chr_be_write(c->uart9_chr, &record, sizeof(record));
        c->type8_feed_count++;
        if (c->type8_feed_count == 1) {
            qemu_log_mask(LOG_UNIMP,
                          "AR-MK2 TYPE8: injected UART9 record 0xF8\n");
        }
    }

    ar_audio_service_tick(c, false);

    physical_memory_read(AR_AUDIO_TIMELINE_ADDR, raw, sizeof(raw));
    timeline = ldl_be_p(raw);
    if (timeline != c->type8_last_timeline) {
        c->type8_last_timeline = timeline;
        if (!c->type8_timeline_seen) {
            c->type8_timeline_seen = true;
            qemu_log_mask(LOG_UNIMP,
                          "AR-MK2 TYPE8: audio timeline advanced to 0x%08x\n",
                          timeline);
        }
    }

    timer_mod_ns(c->type8_timer,
                 qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + AR_TYPE8_PERIOD_NS);
}

static void ar_uart8_init(MemoryRegion *sysmem, ARCoreState *c)
{
    MemoryRegion *mr;

    c->uart8_irq = qemu_allocate_irq(ar_uart8_dma_request, c, 34);
    c->uart8_chr = serial_hd(0);
    c->uart8 = mcf_uart_create(c->uart8_irq, c->uart8_chr);
    mr = sysbus_mmio_get_region(SYS_BUS_DEVICE(c->uart8), 0);
    memory_region_add_subregion_overlap(sysmem, AR_UART8_BASE, mr, 20);

    memory_region_init_io(&c->uart8_proxy, NULL, &ar_uart8_proxy_ops, c,
                          "ar-mk2-uart8-boot-proxy", 0x40);
    memory_region_add_subregion_overlap(sysmem, AR_UART8_BASE,
                                        &c->uart8_proxy, 30);
    qemu_register_reset(ar_uart8_mark_boot_unapplied, c);

}

static void ar_uart9_dma_request(void *opaque, int n, int level)
{
    ARCoreState *c = opaque;

    ar_edma_pump_uarts(c);
}

static void ar_uart9_init(MemoryRegion *sysmem, ARCoreState *c)
{
    MemoryRegion *mr;
    const char *mock_audio = g_getenv("AR_MK2_MOCK_AUDIO_SERVICE");
    const char *trigger_audio = g_getenv("AR_MK2_AUDIO_TRIGGER_SERVICE");

    c->mock_audio_service = mock_audio && *mock_audio &&
                            strcmp(mock_audio, "0") != 0;
    c->trigger_audio_service = trigger_audio && *trigger_audio &&
                               strcmp(trigger_audio, "0") != 0;

    c->uart9_irq = qemu_allocate_irq(ar_uart9_dma_request, c, 36);
    c->uart9_chr = qemu_chr_new("ar-mk2-uart9", "null", NULL);
    c->uart9 = mcf_uart_create(c->uart9_irq, c->uart9_chr);
    mr = sysbus_mmio_get_region(SYS_BUS_DEVICE(c->uart9), 0);
    memory_region_add_subregion_overlap(sysmem, AR_UART9_BASE, mr, 20);

    c->type8_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ar_type8_feed, c);
    c->audio_service_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                          ar_audio_service_feed, c);
    timer_mod_ns(c->type8_timer,
                 qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + AR_TYPE8_START_NS);
    timer_mod_ns(c->audio_service_timer,
                 qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + AR_TYPE8_START_NS);
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
    ar_edma_init(sysmem, ar_core);
    ar_uart8_init(sysmem, ar_core);
    ar_uart9_init(sysmem, ar_core);
}
