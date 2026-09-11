/*
 * Experimental Elektron Analog Rytm MKII machine model for QEMU m68k/ColdFire.
 *
 * Research scaffold only. It is intentionally incomplete: unknown MCF5441x
 * peripherals are trapped by catch-all PBC0/PBC1 MMIO regions and logged.
 *
 * Target firmware assumptions from AR MKII OS 1.72 reverse engineering:
 *   - ColdFire V4-class CPU; QEMU cfv4e is the closest existing CPU model.
 *   - MAIN image is loaded into SDRAM at 0x40000400.
 *   - MAIN entry candidate is 0x40000870.
 *   - firmware switches SP to 0x48000000 very early.
 *   - 64 KiB internal SRAM is used through the 0x80000000 backdoor aperture.
 */

#include "qemu/osdep.h"
#include "qemu/units.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "qemu/log.h"
#include "qemu/datadir.h"
#include "target/m68k/cpu.h"
#include "hw/core/boards.h"
#include "hw/core/loader.h"
#include "system/system.h"
#include "system/address-spaces.h"
#include "system/memory.h"
#include "system/physmem.h"
#include "qemu/audio.h"
#include "qemu/timer.h"

#define AR_MAIN_LOAD_ADDR    0x40000400u
#define AR_MAIN_ENTRY        0x40000870u
#define AR_SDRAM_BASE        0x40000000u
#define AR_DEFAULT_RAM_SIZE  (256 * MiB)
#define AR_SRAM_BASE         0x80000000u
#define AR_SRAM_SIZE         (64 * KiB)
#define AR_SRAM_APERTURE     0x0C000000u
#define AR_PBC1_BASE         0xE0000000u
#define AR_PBC0_BASE         0xF0000000u
#define AR_PBC_WINDOW        0x10000000u
#define AR_BOOT_STACK        0x47FFFFE0u
#define AR_FB_PTR_GLOBAL     0x4026F474u
#define AR_FB_BYTES          0x400u
#define AR_PARAMETER_ADDR    0x8000E5B0u
#define AR_PARAMETER_BYTES   0x54u
#define AR_TRACK_LEVEL_ADDR  0x4123C8E3u
#define AR_TRACK_LEVEL_BYTES 0x1Au
#define AR_SELECTED_TRACK_ADDR 0x412FF96Fu
#define AR_TRACK_LEVEL_STATE_BYTES (4u + AR_TRACK_LEVEL_BYTES)
#define AR_TRIG_STATE_ADDR   0x407C4B93u
#define AR_TRIG_STATE_BYTES  0x0Au
#define AR_GPIO_MEDIA_INPUT  0xEC09401Au
#define AR_GPIO_MEDIA_SET    0xEC09401Bu
#define AR_GPIO_MEDIA_CLEAR  0xEC094027u
#define AR_GPIO_MEDIA_SENSE  0x08u
#define AR_GPIO_MEDIA_DRIVE  0x10u
#define AR_SAMPLE_NAMES       0x41928DCCu
#define AR_SAMPLE_METADATA    0x419289CCu
#define AR_SAMPLE_SECONDARY   0x41928BCCu
#define AR_SAMPLE_STATUS      0x4192894Cu
#define AR_SAMPLE_REGISTRY    0x41310D30u
#define AR_BLANK_NAME         0x40228E97u
#define AR_EKFS_READY         0x4182A6FCu
#define AR_SYNTH_SAMPLE_ADDR  0x4FF00000u
#define AR_SYNTH_NAME_ADDR    0x4FF00400u
#define AR_SYNTH_SAMPLE_SLOT  1u
#define AR_SYNTH_SAMPLE_FRAMES 256u
#define AR_SYNTH_SAMPLE_MAX_FRAMES 48000u
#define AR_RENDER_RING_COUNT   4u
#define AR_RENDER_INDEX_ADDR   0x42F78044u
#define AR_RENDER_BLOCK_BYTES  0x800u
#define AR_RENDER_FRAMES       32u
#define AR_RENDER_FRAME_BYTES  0x40u
#define AR_RENDER_LANES        8u
#define AR_AUDIO_CHANNELS      2u
#define AR_AUDIO_PCM_BYTES     (AR_RENDER_FRAMES * AR_AUDIO_CHANNELS * 2u)

void ar_mk2_intc_pit_init(MemoryRegion *sysmem, M68kCPU *cpu);
void ar_mk2_dspi_init(MemoryRegion *sysmem);

/* Known MCF5441x module bases used only for readable logging. */
typedef struct ARPeripheralName {
    hwaddr base;
    hwaddr size;
    const char *name;
} ARPeripheralName;

static const ARPeripheralName ar_peripherals[] = {
    { 0xEC008000, 0x4000, "1WIRE" },
    { 0xEC010000, 0x4000, "I2C2" },
    { 0xEC014000, 0x4000, "I2C3" },
    { 0xEC018000, 0x4000, "I2C4" },
    { 0xEC01C000, 0x4000, "I2C5" },
    { 0xEC038000, 0x4000, "DSPI2" },
    { 0xEC03C000, 0x4000, "DSPI3" },
    { 0xEC060000, 0x4000, "UART4" },
    { 0xEC064000, 0x4000, "UART5" },
    { 0xEC068000, 0x4000, "UART6" },
    { 0xEC06C000, 0x4000, "UART7" },
    { 0xEC070000, 0x4000, "UART8" },
    { 0xEC074000, 0x4000, "UART9" },
    { 0xEC088000, 0x4000, "mcPWM" },
    { 0xEC090000, 0x4000, "CCM" },
    { 0xEC094000, 0x4000, "GPIO/PINMUX" },

    { 0xFC004000, 0x4000, "XBAR" },
    { 0xFC008000, 0x4000, "FLEXBUS" },
    { 0xFC020000, 0x4000, "FLEXCAN0" },
    { 0xFC024000, 0x4000, "FLEXCAN1" },
    { 0xFC038000, 0x4000, "I2C1" },
    { 0xFC03C000, 0x4000, "DSPI1" },
    { 0xFC040000, 0x4000, "SCM" },
    { 0xFC044000, 0x4000, "eDMA" },
    { 0xFC048000, 0x4000, "INTC0" },
    { 0xFC04C000, 0x4000, "INTC1" },
    { 0xFC050000, 0x4000, "INTC2" },
    { 0xFC054000, 0x4000, "IACK" },
    { 0xFC058000, 0x4000, "I2C0" },
    { 0xFC05C000, 0x4000, "DSPI0" },
    { 0xFC060000, 0x4000, "UART0" },
    { 0xFC064000, 0x4000, "UART1" },
    { 0xFC068000, 0x4000, "UART2" },
    { 0xFC06C000, 0x4000, "UART3" },
    { 0xFC070000, 0x4000, "DTIM0" },
    { 0xFC074000, 0x4000, "DTIM1" },
    { 0xFC078000, 0x4000, "DTIM2" },
    { 0xFC07C000, 0x4000, "DTIM3" },
    { 0xFC080000, 0x4000, "PIT0" },
    { 0xFC084000, 0x4000, "PIT1" },
    { 0xFC088000, 0x4000, "PIT2" },
    { 0xFC08C000, 0x4000, "PIT3" },
    { 0xFC090000, 0x4000, "EPORT0" },
    { 0xFC094000, 0x4000, "ADC" },
    { 0xFC098000, 0x4000, "DAC0" },
    { 0xFC09C000, 0x4000, "DAC1" },
    { 0xFC0A8000, 0x4000, "RTC" },
    { 0xFC0AC000, 0x4000, "SIM" },
    { 0xFC0B0000, 0x4000, "USB-OTG" },
    { 0xFC0B4000, 0x4000, "USB-HOST" },
    { 0xFC0B8000, 0x4000, "DDR" },
    { 0xFC0BC000, 0x4000, "SSI0" },
    { 0xFC0C0000, 0x4000, "PLL" },
    { 0xFC0C4000, 0x4000, "RNG" },
    { 0xFC0C8000, 0x4000, "SSI1" },
    { 0xFC0CC000, 0x4000, "eSDHC" },
    { 0xFC0D4000, 0x4000, "MAC-NET0" },
    { 0xFC0D8000, 0x4000, "MAC-NET1" },
};

typedef struct ARBoardState {
    M68kCPU *cpu;
    MemoryRegion sram_backdoor;
    uint8_t sram_bytes[AR_SRAM_SIZE];
    MemoryRegion pbc0;
    MemoryRegion pbc1;
    uint64_t mmio_reads;
    uint64_t mmio_writes;
    GHashTable *mmio_bytes; /* sparse byte-addressed register backing */
    QEMUTimer *frame_timer;
    QEMUTimer *sample_timer;
    AudioBackend *audio_be;
    SWVoiceOut *audio_voice;
    uint8_t audio_candidate[AR_RENDER_RING_COUNT][AR_RENDER_BLOCK_BYTES];
    uint8_t audio_published[AR_RENDER_RING_COUNT][AR_RENDER_BLOCK_BYTES];
    bool audio_candidate_valid[AR_RENDER_RING_COUNT];
    bool audio_published_valid[AR_RENDER_RING_COUNT];
    uint32_t audio_candidate_ring;
    uint32_t audio_last_ring;
    bool audio_last_ring_valid;
    uint8_t audio_pcm[AR_AUDIO_PCM_BYTES];
    size_t audio_pcm_pos;
    size_t audio_pcm_len;
    bool audio_tap;
    bool audio_nonzero_seen;
    char *audio_block_out;
    char *frame_out;
    uint8_t frame_candidate[AR_FB_BYTES];
    uint8_t frame_published[AR_FB_BYTES];
    uint32_t frame_candidate_ptr;
    unsigned frame_candidate_matches;
    bool frame_candidate_valid;
    bool frame_published_valid;
    char *parameter_out;
    uint8_t parameter_published[AR_PARAMETER_BYTES];
    bool parameter_published_valid;
    char *track_level_out;
    uint8_t track_level_published[AR_TRACK_LEVEL_STATE_BYTES];
    bool track_level_published_valid;
    char *trig_state_out;
    uint8_t trig_state_published[AR_TRIG_STATE_BYTES];
    bool trig_state_published_valid;
    bool mock_factory_state;
    bool mock_project_sample;
    unsigned mock_project_sample_frames;
    bool media_probe_high;
} ARBoardState;

static bool ar_renderer_block_nonzero(const uint8_t *block)
{
    unsigned i;

    for (i = 0; i < AR_RENDER_BLOCK_BYTES; i++) {
        if (block[i]) {
            return true;
        }
    }
    return false;
}

static void ar_mix_renderer_block(ARBoardState *s, const uint8_t *block)
{
    unsigned frame;

    for (frame = 0; frame < AR_RENDER_FRAMES; frame++) {
        int64_t mixed = 0;
        int32_t sample;
        unsigned lane;

        for (lane = 0; lane < AR_RENDER_LANES; lane++) {
            const uint8_t *src = block + frame * AR_RENDER_FRAME_BYTES +
                                 lane * sizeof(uint32_t);
            mixed += (int32_t)ldl_be_p(src);
        }

        /* The renderer stores signed samples with six fractional/guard bits.
         * Sum the physical lanes, retain natural saturation, and mirror the
         * result to stereo until the hardware pan/return mapping is known. */
        mixed >>= 6;
        sample = CLAMP(mixed, INT16_MIN, INT16_MAX);
        stw_le_p(s->audio_pcm + frame * 4, (uint16_t)sample);
        stw_le_p(s->audio_pcm + frame * 4 + 2, (uint16_t)sample);
    }
    s->audio_pcm_pos = 0;
    s->audio_pcm_len = sizeof(s->audio_pcm);
}

static bool ar_capture_renderer_block(ARBoardState *s)
{
    uint8_t raw[4];
    uint32_t ring;
    const uint8_t *block;
    bool new_generation;
    bool changed_content;

    physical_memory_read(AR_RENDER_INDEX_ADDR, raw, sizeof(raw));
    ring = ldl_be_p(raw);
    if (ring >= AR_RENDER_RING_COUNT) {
        return false;
    }
    block = s->sram_bytes + ring * AR_RENDER_BLOCK_BYTES;

    /* Observe the same selector and bytes twice before publishing so the host
     * never consumes a renderer block while the guest is still filling it. */
    if (!s->audio_candidate_valid[ring] ||
        s->audio_candidate_ring != ring ||
        memcmp(s->audio_candidate[ring], block, AR_RENDER_BLOCK_BYTES) != 0) {
        memcpy(s->audio_candidate[ring], block, AR_RENDER_BLOCK_BYTES);
        s->audio_candidate_valid[ring] = true;
        s->audio_candidate_ring = ring;
        return false;
    }

    new_generation = !s->audio_last_ring_valid || s->audio_last_ring != ring;
    changed_content = !s->audio_published_valid[ring] ||
                      memcmp(s->audio_published[ring], block,
                             AR_RENDER_BLOCK_BYTES) != 0;
    s->audio_last_ring = ring;
    s->audio_last_ring_valid = true;
    if (!new_generation && !changed_content) {
        return false;
    }
    memcpy(s->audio_published[ring], block, AR_RENDER_BLOCK_BYTES);
    s->audio_published_valid[ring] = true;
    if (!ar_renderer_block_nonzero(block)) {
        return false;
    }

    ar_mix_renderer_block(s, block);
    if (!s->audio_nonzero_seen) {
        if (s->audio_block_out &&
            !g_file_set_contents(s->audio_block_out, (const char *)block,
                                 AR_RENDER_BLOCK_BYTES, NULL)) {
            qemu_log_mask(LOG_GUEST_ERROR,
                          "AR-MK2: failed to write first audio block %s\n",
                          s->audio_block_out);
        }
        s->audio_nonzero_seen = true;
        qemu_log_mask(LOG_GUEST_ERROR,
                      "AR-MK2 AUDIO: streaming stock renderer ring %u\n",
                      ring);
    }
    return true;
}

static void ar_audio_callback(void *opaque, int avail)
{
    ARBoardState *s = opaque;
    uint8_t silence[256] = { 0 };

    while (avail > 0) {
        size_t remaining;
        size_t requested;
        size_t written;

        if (s->audio_pcm_pos == s->audio_pcm_len) {
            s->audio_pcm_pos = 0;
            s->audio_pcm_len = 0;
            ar_capture_renderer_block(s);
        }
        remaining = s->audio_pcm_len - s->audio_pcm_pos;
        if (remaining) {
            requested = MIN(remaining, (size_t)avail);
            written = audio_be_write(s->audio_be, s->audio_voice,
                                     s->audio_pcm + s->audio_pcm_pos,
                                     requested);
            s->audio_pcm_pos += written;
        } else {
            requested = MIN(sizeof(silence), (size_t)avail);
            written = audio_be_write(s->audio_be, s->audio_voice,
                                     silence, requested);
        }
        if (!written) {
            break;
        }
        avail -= written;
    }
}

static void ar_audio_init(ARBoardState *s)
{
    struct audsettings settings = {
        .freq = 48000,
        .nchannels = AR_AUDIO_CHANNELS,
        .fmt = AUDIO_FORMAT_S16,
        .big_endian = false,
    };
    Error *local_err = NULL;

    s->audio_be = audio_get_default_audio_be(&local_err);
    if (!s->audio_be) {
        error_report_err(local_err);
        return;
    }
    s->audio_voice = audio_be_open_out(s->audio_be, NULL,
                                       "ar-mk2-renderer", s,
                                       ar_audio_callback, &settings);
    if (!s->audio_voice) {
        error_report("AR-MK2: could not open renderer audio voice");
        return;
    }
    audio_be_set_active_out(s->audio_be, s->audio_voice, true);
}

static const char *ar_mmio_name(hwaddr absolute)
{
    size_t i;
    for (i = 0; i < ARRAY_SIZE(ar_peripherals); i++) {
        if (absolute >= ar_peripherals[i].base &&
            absolute < ar_peripherals[i].base + ar_peripherals[i].size) {
            return ar_peripherals[i].name;
        }
    }
    return "UNKNOWN";
}

static uint64_t ar_sram_read(void *opaque, hwaddr addr, unsigned size)
{
    ARBoardState *s = opaque;
    uint64_t value = 0;
    unsigned i;
    for (i = 0; i < size; i++) {
        value = (value << 8) | s->sram_bytes[(addr + i) & (AR_SRAM_SIZE - 1)];
    }
    return value;
}

static void ar_sram_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ARBoardState *s = opaque;
    unsigned i;
    for (i = 0; i < size; i++) {
        unsigned shift = 8 * (size - 1 - i);
        s->sram_bytes[(addr + i) & (AR_SRAM_SIZE - 1)] = (uint8_t)(value >> shift);
    }
}

static const MemoryRegionOps ar_sram_ops = {
    .read = ar_sram_read,
    .write = ar_sram_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static uint8_t ar_sparse_get_byte(ARBoardState *s, hwaddr addr)
{
    gpointer p = g_hash_table_lookup(s->mmio_bytes, GUINT_TO_POINTER((guint)addr));
    return p ? (uint8_t)(GPOINTER_TO_UINT(p) - 1) : 0;
}

static void ar_sparse_put_byte(ARBoardState *s, hwaddr addr, uint8_t value)
{
    /* Store value+1 because NULL represents an absent key in lookup(). */
    g_hash_table_insert(s->mmio_bytes, GUINT_TO_POINTER((guint)addr),
                        GUINT_TO_POINTER((guint)value + 1));
}

static uint64_t ar_default_mmio_read(void *opaque, hwaddr offset, unsigned size,
                                     hwaddr bus_base)
{
    ARBoardState *s = opaque;
    hwaddr absolute = bus_base + offset;
    uint64_t value = 0;
    unsigned i;

    s->mmio_reads++;
    for (i = 0; i < size; i++) {
        value = (value << 8) | ar_sparse_get_byte(s, absolute + i);
    }

    /* The storage probe drives one GPIO and samples its board-level loopback.
     * Model that wiring only for the explicitly requested synthetic factory
     * state; the default catch-all remains passive. */
    if (s->mock_factory_state && absolute == AR_GPIO_MEDIA_INPUT && size == 1) {
        value = (value & ~AR_GPIO_MEDIA_SENSE) |
                (s->media_probe_high ? AR_GPIO_MEDIA_SENSE : 0);
    }

    qemu_log_mask(LOG_UNIMP,
                  "AR-MK2 MMIO R pc=%08x addr=%08" HWADDR_PRIx
                  " size=%u value=%08" PRIx64 " module=%s\n",
                  s->cpu ? s->cpu->env.pc : 0,
                  absolute, size, value, ar_mmio_name(absolute));
    return value;
}

static void ar_default_mmio_write(void *opaque, hwaddr offset, uint64_t value,
                                  unsigned size, hwaddr bus_base)
{
    ARBoardState *s = opaque;
    hwaddr absolute = bus_base + offset;
    unsigned i;

    s->mmio_writes++;
    for (i = 0; i < size; i++) {
        unsigned shift = 8 * (size - 1 - i);
        ar_sparse_put_byte(s, absolute + i, (uint8_t)(value >> shift));
    }

    if (s->mock_factory_state && size == 1) {
        if (absolute == AR_GPIO_MEDIA_SET && (value & AR_GPIO_MEDIA_DRIVE)) {
            s->media_probe_high = true;
        } else if (absolute == AR_GPIO_MEDIA_CLEAR &&
                   !(value & AR_GPIO_MEDIA_DRIVE)) {
            s->media_probe_high = false;
        }
    }

    qemu_log_mask(LOG_UNIMP,
                  "AR-MK2 MMIO W pc=%08x addr=%08" HWADDR_PRIx
                  " size=%u value=%08" PRIx64 " module=%s\n",
                  s->cpu ? s->cpu->env.pc : 0,
                  absolute, size, value, ar_mmio_name(absolute));
}

static uint64_t ar_pbc0_read(void *opaque, hwaddr addr, unsigned size)
{
    return ar_default_mmio_read(opaque, addr, size, AR_PBC0_BASE);
}

static void ar_pbc0_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ar_default_mmio_write(opaque, addr, value, size, AR_PBC0_BASE);
}

static uint64_t ar_pbc1_read(void *opaque, hwaddr addr, unsigned size)
{
    return ar_default_mmio_read(opaque, addr, size, AR_PBC1_BASE);
}

static void ar_pbc1_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ar_default_mmio_write(opaque, addr, value, size, AR_PBC1_BASE);
}

static const MemoryRegionOps ar_pbc0_ops = {
    .read = ar_pbc0_read,
    .write = ar_pbc0_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static const MemoryRegionOps ar_pbc1_ops = {
    .read = ar_pbc1_read,
    .write = ar_pbc1_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void ar_export_framebuffer(void *opaque)
{
    ARBoardState *s = opaque;
    uint8_t pbuf[4];
    uint8_t frame[AR_FB_BYTES];
    uint32_t ptr;

    if (s->frame_out) {
        physical_memory_read(AR_FB_PTR_GLOBAL, pbuf, sizeof(pbuf));
        ptr = ldl_be_p(pbuf);
        if (ptr >= AR_SDRAM_BASE &&
            (uint64_t)ptr + AR_FB_BYTES <= AR_SDRAM_BASE + AR_DEFAULT_RAM_SIZE) {
            physical_memory_read(ptr, frame, sizeof(frame));

        /*
         * Firmware redraws over multiple scheduler slices. Publishing every
         * sample exposes those intermediate writes as torn desktop frames.
         * Require the pointer and all 1024 bytes to match twice in succession,
         * then write only when the stable image differs from the last export.
         */
            if (s->frame_candidate_valid &&
                s->frame_candidate_ptr == ptr &&
                memcmp(s->frame_candidate, frame, sizeof(frame)) == 0) {
                if (s->frame_candidate_matches < UINT_MAX) {
                    s->frame_candidate_matches++;
                }
            } else {
                memcpy(s->frame_candidate, frame, sizeof(frame));
                s->frame_candidate_ptr = ptr;
                s->frame_candidate_matches = 0;
                s->frame_candidate_valid = true;
            }

            if (s->frame_candidate_matches >= 1 &&
                (!s->frame_published_valid ||
                 memcmp(s->frame_published, frame, sizeof(frame)) != 0)) {
                if (!g_file_set_contents(s->frame_out, (const char *)frame,
                                         sizeof(frame), NULL)) {
                    qemu_log_mask(LOG_GUEST_ERROR,
                                  "AR-MK2: failed to write framebuffer file %s\n",
                                  s->frame_out);
                } else {
                    memcpy(s->frame_published, frame, sizeof(frame));
                    s->frame_published_valid = true;
                }
            }
        }
    }

    if (s->parameter_out) {
        uint8_t parameters[AR_PARAMETER_BYTES];

        physical_memory_read(AR_PARAMETER_ADDR, parameters, sizeof(parameters));
        if (!s->parameter_published_valid ||
            memcmp(s->parameter_published, parameters, sizeof(parameters)) != 0) {
            if (!g_file_set_contents(s->parameter_out,
                                     (const char *)parameters,
                                     sizeof(parameters), NULL)) {
                qemu_log_mask(LOG_GUEST_ERROR,
                              "AR-MK2: failed to write parameter-state file %s\n",
                              s->parameter_out);
            } else {
                memcpy(s->parameter_published, parameters, sizeof(parameters));
                s->parameter_published_valid = true;
            }
        }
    }

    if (s->track_level_out) {
        uint8_t levels[AR_TRACK_LEVEL_STATE_BYTES];

        physical_memory_read(AR_SELECTED_TRACK_ADDR, levels, 4);
        physical_memory_read(AR_TRACK_LEVEL_ADDR, levels + 4,
                             AR_TRACK_LEVEL_BYTES);
        if (!s->track_level_published_valid ||
            memcmp(s->track_level_published, levels, sizeof(levels)) != 0) {
            if (!g_file_set_contents(s->track_level_out,
                                     (const char *)levels,
                                     sizeof(levels), NULL)) {
                qemu_log_mask(LOG_GUEST_ERROR,
                              "AR-MK2: failed to write track-level file %s\n",
                              s->track_level_out);
            } else {
                memcpy(s->track_level_published, levels, sizeof(levels));
                s->track_level_published_valid = true;
            }
        }
    }

    if (s->trig_state_out) {
        uint8_t trig[AR_TRIG_STATE_BYTES];

        physical_memory_read(AR_TRIG_STATE_ADDR, trig, sizeof(trig));
        if (!s->trig_state_published_valid ||
            memcmp(s->trig_state_published, trig, sizeof(trig)) != 0) {
            if (!g_file_set_contents(s->trig_state_out, (const char *)trig,
                                     sizeof(trig), NULL)) {
                qemu_log_mask(LOG_GUEST_ERROR,
                              "AR-MK2: failed to write trig-state file %s\n",
                              s->trig_state_out);
            } else {
                memcpy(s->trig_state_published, trig, sizeof(trig));
                s->trig_state_published_valid = true;
            }
        }
    }

    timer_mod(s->frame_timer, qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 16);
}

static void ar_inject_project_sample(void *opaque)
{
    ARBoardState *s = opaque;
    uint8_t word[4];
    g_autofree uint8_t *pcm = NULL;
    uint8_t registry[16] = { 0 };
    static const uint8_t name[16] = "QEMU TEST";
    uint32_t name_ptr;
    uint32_t metadata;
    uint32_t ekfs_ready;
    unsigned i;

    physical_memory_read(AR_EKFS_READY, word, sizeof(word));
    ekfs_ready = ldl_be_p(word);
    if (ekfs_ready == 0) {
        timer_mod(s->sample_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 100);
        return;
    }

    physical_memory_read(AR_SAMPLE_NAMES + AR_SYNTH_SAMPLE_SLOT * 4,
                         word, sizeof(word));
    name_ptr = ldl_be_p(word);
    if (name_ptr == AR_SYNTH_NAME_ADDR) {
        timer_mod(s->sample_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 1000);
        return;
    }
    if (name_ptr == 0) {
        timer_mod(s->sample_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 100);
        return;
    }
    physical_memory_read(AR_SAMPLE_METADATA + AR_SYNTH_SAMPLE_SLOT * 4,
                         word, sizeof(word));
    metadata = ldl_be_p(word);
    if (name_ptr != AR_BLANK_NAME || metadata != 0) {
        qemu_log_mask(LOG_GUEST_ERROR,
                      "AR-MK2: refusing synthetic sample overwrite for slot %u\n",
                      AR_SYNTH_SAMPLE_SLOT);
        return;
    }

    pcm = g_malloc(s->mock_project_sample_frames * 2u);
    for (i = 0; i < s->mock_project_sample_frames; i++) {
        int16_t sample = (i / 16) & 1 ? 0x5000 : -0x5000;
        stw_be_p(pcm + i * 2, (uint16_t)sample);
    }
    stl_be_p(registry, AR_SYNTH_SAMPLE_ADDR);
    stw_be_p(registry + 4, 48000);
    stl_be_p(registry + 8, s->mock_project_sample_frames);
    stl_be_p(registry + 12, 0x40000000u);

    physical_memory_write(AR_SYNTH_SAMPLE_ADDR, pcm,
                          s->mock_project_sample_frames * 2u);
    physical_memory_write(AR_SYNTH_NAME_ADDR, name, sizeof(name));
    physical_memory_write(AR_SAMPLE_REGISTRY + AR_SYNTH_SAMPLE_SLOT * 16,
                          registry, sizeof(registry));

    stl_be_p(word, s->mock_project_sample_frames * 2u);
    physical_memory_write(AR_SAMPLE_METADATA + AR_SYNTH_SAMPLE_SLOT * 4,
                          word, sizeof(word));
    stl_be_p(word, 0);
    physical_memory_write(AR_SAMPLE_SECONDARY + AR_SYNTH_SAMPLE_SLOT * 4,
                          word, sizeof(word));
    word[0] = 0xff;
    physical_memory_write(AR_SAMPLE_STATUS + AR_SYNTH_SAMPLE_SLOT,
                          word, 1);
    stl_be_p(word, AR_SYNTH_NAME_ADDR);
    physical_memory_write(AR_SAMPLE_NAMES + AR_SYNTH_SAMPLE_SLOT * 4,
                          word, sizeof(word));

    qemu_log_mask(LOG_GUEST_ERROR,
                  "AR-MK2: injected generated 16-bit test sample in slot %u "
                  "frames=%u\n",
                  AR_SYNTH_SAMPLE_SLOT, s->mock_project_sample_frames);
    timer_mod(s->sample_timer,
              qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 1000);
}

static void ar_write_boot_argument(MachineState *machine)
{
    uint8_t *ram = memory_region_get_ram_ptr(machine->ram);
    hwaddr off = AR_BOOT_STACK - AR_SDRAM_BASE;
    uint32_t flags = 0x10;
    const char *env_flags = g_getenv("AR_MK2_BOOT_FLAGS");

    /* MAIN copies the long at incoming SP+4 directly into its boot-mode global.
     * Normal UI initialization needs bit 0x10 when booting a raw MAIN without
     * the physical bootloader/project state. Keep it overrideable for research. */
    if (env_flags && *env_flags) {
        char *endp = NULL;
        uint64_t parsed = g_ascii_strtoull(env_flags, &endp, 0);
        if (endp && *endp == '\0' && parsed <= UINT32_MAX) {
            flags = parsed;
        }
    }
    stl_be_p(ram + off + 4, flags);
}

static void elektron_ar_mk2_init(MachineState *machine)
{
    MemoryRegion *sysmem = get_system_memory();
    ARBoardState *s = g_new0(ARBoardState, 1);
    CPUM68KState *env;
    char *fn;
    int64_t loaded;

    if (machine->ram_size < AR_DEFAULT_RAM_SIZE) {
        error_report("elektron-ar-mk2 currently requires at least 256 MiB RAM");
        exit(1);
    }

    s->mmio_bytes = g_hash_table_new(g_direct_hash, g_direct_equal);
    {
        const char *mock = g_getenv("AR_MK2_MOCK_FACTORY_STATE");
        s->mock_factory_state = mock && *mock && strcmp(mock, "0") != 0;
    }
    {
        const char *mock = g_getenv("AR_MK2_MOCK_PROJECT_SAMPLE");
        s->mock_project_sample = mock && *mock && strcmp(mock, "0") != 0;
    }
    s->mock_project_sample_frames = AR_SYNTH_SAMPLE_FRAMES;
    {
        const char *frames = g_getenv("AR_MK2_MOCK_PROJECT_SAMPLE_FRAMES");

        if (frames && *frames) {
            char *endp = NULL;
            uint64_t parsed = g_ascii_strtoull(frames, &endp, 0);

            if (endp && *endp == '\0' && parsed > 0 &&
                parsed <= AR_SYNTH_SAMPLE_MAX_FRAMES) {
                s->mock_project_sample_frames = parsed;
            } else {
                qemu_log_mask(LOG_GUEST_ERROR,
                              "AR-MK2: invalid synthetic sample frame count "
                              "'%s'; using %u\n",
                              frames, AR_SYNTH_SAMPLE_FRAMES);
            }
        }
    }
    {
        const char *out = g_getenv("AR_MK2_FRAMEBUFFER_OUT");
        if (out && *out) {
            s->frame_out = g_strdup(out);
        }
    }
    {
        const char *out = g_getenv("AR_MK2_PARAMETER_STATE_OUT");
        if (out && *out) {
            s->parameter_out = g_strdup(out);
        }
    }
    {
        const char *out = g_getenv("AR_MK2_TRACK_LEVEL_STATE_OUT");
        if (out && *out) {
            s->track_level_out = g_strdup(out);
        }
    }
    {
        const char *out = g_getenv("AR_MK2_TRIG_STATE_OUT");
        if (out && *out) {
            s->trig_state_out = g_strdup(out);
        }
    }
    {
        const char *tap = g_getenv("AR_MK2_AUDIO_TAP");
        s->audio_tap = tap && *tap && strcmp(tap, "0") != 0;
    }
    {
        const char *out = g_getenv("AR_MK2_AUDIO_BLOCK_OUT");
        if (out && *out) {
            s->audio_block_out = g_strdup(out);
        }
    }
    s->cpu = M68K_CPU(cpu_create(machine->cpu_type));
    env = &s->cpu->env;

    /* External SDRAM. */
    memory_region_add_subregion(sysmem, AR_SDRAM_BASE, machine->ram);

    /* MCF5441x internal SRAM backdoor. The 64 KiB physical SRAM repeats
     * throughout the 0x80000000..0x8BFFFFFF aperture, so mask every access
     * to the 16-bit physical SRAM offset instead of allocating the aperture. */
    memory_region_init_io(&s->sram_backdoor, NULL, &ar_sram_ops, s,
                          "elektron-ar-mk2.sram-backdoor", AR_SRAM_APERTURE);
    memory_region_add_subregion(sysmem, AR_SRAM_BASE, &s->sram_backdoor);

    /* Catch-all peripheral buses. Specific modules can later be overlaid with
     * higher-priority regions as their behavior becomes necessary. */
    memory_region_init_io(&s->pbc1, NULL, &ar_pbc1_ops, s,
                          "elektron-ar-mk2.pbc1", AR_PBC_WINDOW);
    memory_region_add_subregion(sysmem, AR_PBC1_BASE, &s->pbc1);
    memory_region_init_io(&s->pbc0, NULL, &ar_pbc0_ops, s,
                          "elektron-ar-mk2.pbc0", AR_PBC_WINDOW);
    memory_region_add_subregion(sysmem, AR_PBC0_BASE, &s->pbc0);

    /* Overlay the first stateful MCF5441x blocks on the discovery buses. */
    ar_mk2_intc_pit_init(sysmem, s->cpu);
    ar_mk2_dspi_init(sysmem);

    if (!machine->firmware) {
        error_report("Use -bios <decompressed-main.bin> for AR MKII research firmware");
        exit(1);
    }

    fn = qemu_find_file(QEMU_FILE_TYPE_BIOS, machine->firmware);
    if (!fn) {
        error_report("Could not find AR MKII MAIN image '%s'", machine->firmware);
        exit(1);
    }
    loaded = load_image_targphys(fn, AR_MAIN_LOAD_ADDR,
                                 machine->ram_size - (AR_MAIN_LOAD_ADDR - AR_SDRAM_BASE),
                                 NULL);
    g_free(fn);
    if (loaded <= 0) {
        error_report("Could not load AR MKII MAIN image into SDRAM");
        exit(1);
    }

    ar_write_boot_argument(machine);

    /* QEMU m68k reset does not load vectors automatically; this image is not a
     * conventional reset-vector ROM anyway. Seed the observed MAIN entry and a
     * synthetic incoming boot stack directly. */
    env->vbr = 0;
    env->aregs[7] = AR_BOOT_STACK;
    env->pc = AR_MAIN_ENTRY;

    if (s->frame_out || s->parameter_out || s->track_level_out ||
        s->trig_state_out) {
        s->frame_timer = timer_new_ms(QEMU_CLOCK_VIRTUAL, ar_export_framebuffer, s);
        timer_mod(s->frame_timer, qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 16);
    }
    if (s->mock_project_sample) {
        s->sample_timer = timer_new_ms(QEMU_CLOCK_VIRTUAL,
                                       ar_inject_project_sample, s);
        timer_mod(s->sample_timer, qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 100);
    }
    if (s->audio_tap) {
        ar_audio_init(s);
    }

    qemu_log_mask(LOG_UNIMP,
                  "AR-MK2: loaded %" PRId64 " bytes @ %08x; PC=%08x SP=%08x\n",
                  loaded, AR_MAIN_LOAD_ADDR, env->pc, env->aregs[7]);
}

static void elektron_ar_mk2_machine_init(MachineClass *mc)
{
    mc->desc = "Elektron Analog Rytm MKII research machine (incomplete)";
    mc->init = elektron_ar_mk2_init;
    mc->default_cpu_type = M68K_CPU_TYPE_NAME("any");
    mc->default_ram_size = AR_DEFAULT_RAM_SIZE;
    mc->default_ram_id = "elektron-ar-mk2.sdram";
}

DEFINE_MACHINE("elektron-ar-mk2", elektron_ar_mk2_machine_init)
