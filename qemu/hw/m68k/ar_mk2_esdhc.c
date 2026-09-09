/*
 * Minimal eSDHC/eMMC model for Analog Rytm MKII emulator factory state.
 *
 * This device is deliberately enabled only by AR_MK2_MOCK_FACTORY_STATE.
 * It provides just enough card/controller behavior for the untouched OS 1.72
 * storage driver to initialize an eMMC and read reconstructed factory metadata.
 * Hardware-validation runs must leave the flag unset.
 */

#include "qemu/osdep.h"
#include "qemu/log.h"
#include "system/memory.h"
#include "system/physmem.h"

#define AR_ESDHC_BASE          0xFC0CC000u
#define AR_ESDHC_SIZE          0x4000u
#define AR_EDMA_ERQH           0xFC044008u
#define AR_EDMA_SSRT           0xFC04401Eu
#define AR_EDMA_CH             59u
#define AR_INTC2_IFRL          0xFC050014u
#define AR_INTC2_ESDHC_SOURCE  31u

#define AR_ESDHC_BLKATTR       0x04u
#define AR_ESDHC_DSADDR        0x08u
#define AR_ESDHC_XFERTYP       0x0Cu
#define AR_ESDHC_CMDRSP0       0x10u
#define AR_ESDHC_CMDRSP1       0x14u
#define AR_ESDHC_CMDRSP2       0x18u
#define AR_ESDHC_CMDRSP3       0x1Cu
#define AR_ESDHC_DATPORT       0x20u
#define AR_ESDHC_PRSSTAT       0x24u
#define AR_ESDHC_PROCTL        0x28u
#define AR_ESDHC_SYSCTL        0x2Cu
#define AR_ESDHC_IRQSTAT       0x30u
#define AR_ESDHC_IRQSTATEN     0x34u
#define AR_ESDHC_IRQSIGEN      0x38u
#define AR_ESDHC_WML           0x44u

#define AR_IRQ_CC              (1u << 0)
#define AR_IRQ_TC              (1u << 1)
#define AR_PRSSTAT_DAT0        (1u << 24)
#define AR_PRSSTAT_BWEN        (1u << 10)
#define AR_PRSSTAT_BREN        (1u << 11)
#define AR_PRSSTAT_DLSL0       (1u << 3)

#define AR_SECTOR_SIZE         512u
#define AR_EMMC_SECTORS        0x003B0000u /* Toshiba 004GE0 active partition */
#define AR_COKI_PRIMARY_SECTOR 0x0007A000u
#define AR_MAJG_SECTOR         0x00180000u
#define AR_EKFS_SECTOR         0x001C0000u

#define AR_CMD_SEND_OP_COND    1u
#define AR_CMD_ALL_SEND_CID    2u
#define AR_CMD_SEND_EXT_CSD    8u
#define AR_CMD_SEND_CID        10u
#define AR_CMD_TUNING_READ     14u
#define AR_CMD_READ_MULTIPLE   18u
#define AR_CMD_TUNING_WRITE    19u
#define AR_CMD_WRITE_SINGLE    24u
#define AR_CMD_WRITE_MULTIPLE  25u
#define AR_CMD_ERASE_START     35u
#define AR_CMD_ERASE_END       36u
#define AR_CMD_ERASE           38u
#define AR_XFERTYP_DPSEL       (1u << 21)

#define AR_STREAM_MAX          65536u

typedef struct AREsdhcState {
    MemoryRegion iomem;
    uint32_t blkattr;
    uint32_t argument;
    uint32_t xfertyp;
    uint32_t response[4];
    uint32_t proctl;
    uint32_t sysctl;
    uint32_t irqstat;
    uint32_t irqstaten;
    uint32_t irqsigen;
    uint32_t wml;
    uint8_t stream[AR_STREAM_MAX];
    size_t stream_len;
    size_t stream_pos;
    uint32_t pio_latch;
    GHashTable *media;
    uint32_t erase_start;
    uint32_t erase_end;
    bool storage_write;
} AREsdhcState;

static AREsdhcState *ar_esdhc;

/* Captured from the stock firmware's own default-record constructor. */
static const uint8_t ar_coki_default[0x74] = {
    [0x00] = 'C', [0x01] = 'O', [0x02] = 'K', [0x03] = 'I',
    [0x04] = 0x35, [0x05] = 0xb6, [0x06] = 0x2a, [0x07] = 0x6d,
    [0x0b] = 0x0c, [0x0f] = 0x64,
    [0x1c] = 'A', [0x1d] = 'n', [0x1e] = 'a', [0x1f] = 'l',
    [0x20] = 'o', [0x21] = 'g', [0x22] = ' ', [0x23] = 'R',
    [0x24] = 'y', [0x25] = 't', [0x26] = 'm',
    [0x3f] = 0x01,
    [0x48] = 0x13, [0x49] = 0x88,
    [0x4c] = 0x13, [0x4d] = 0x88,
    [0x50] = 0x13, [0x51] = 0x88,
    [0x56] = 0x13, [0x57] = 0x88,
    [0x5a] = 0x13, [0x5b] = 0x88,
    [0x5e] = 0x13, [0x5f] = 0x88,
    [0x60] = 0x20, [0x61] = 0x21,
    [0x62] = 0xff, [0x63] = 0xff, [0x64] = 0xff,
    [0x65] = 0xff, [0x66] = 0xff, [0x67] = 0xff,
    [0x68] = 0xa0, [0x69] = 0xa1,
    [0x6a] = 0xff, [0x6b] = 0xff, [0x6c] = 0xff,
    [0x6d] = 0xff, [0x6e] = 0xff, [0x6f] = 0xff,
    [0x70] = 0x10, [0x71] = 0x10,
};

static void ar_store_be32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24;
    p[1] = v >> 16;
    p[2] = v >> 8;
    p[3] = v;
}

static bool ar_edma59_enabled(void)
{
    uint8_t raw[4];
    physical_memory_read(AR_EDMA_ERQH, raw, sizeof(raw));
    return (ldl_be_p(raw) & (1u << (AR_EDMA_CH - 32))) != 0;
}

static void ar_esdhc_set_irq(bool level)
{
    uint8_t raw[4];
    uint32_t ifr;

    physical_memory_read(AR_INTC2_IFRL, raw, sizeof(raw));
    ifr = ldl_be_p(raw);
    if (level) {
        ifr |= 1u << AR_INTC2_ESDHC_SOURCE;
    } else {
        ifr &= ~(1u << AR_INTC2_ESDHC_SOURCE);
    }
    stl_be_p(raw, ifr);
    physical_memory_write(AR_INTC2_IFRL, raw, sizeof(raw));
}

static void ar_esdhc_update_irq(AREsdhcState *s)
{
    ar_esdhc_set_irq((s->irqstat & s->irqsigen) != 0);
}

static void ar_virtual_sector(AREsdhcState *s, uint32_t sector,
                              uint8_t out[AR_SECTOR_SIZE])
{
    const uint8_t *stored;

    memset(out, 0, AR_SECTOR_SIZE);

    if (sector == AR_COKI_PRIMARY_SECTOR) {
        memcpy(out, ar_coki_default, sizeof(ar_coki_default));
        return;
    }

    if (sector == AR_EKFS_SECTOR) {
        memcpy(out, "ekFS", 4);
        /* OS 1.72 hash(0..0x1fb, seed 0x31323334) for this zero-filled
         * minimal superblock, computed by the firmware routine itself. */
        ar_store_be32(out + 0x1fc, 0x655605ECu);
        return;
    }

    if (sector == AR_MAJG_SECTOR) {
        memcpy(out, "MaGj", 4);
        /* These four bytes make the firmware's two-stage reflected CRC32
         * chain terminate at its required residue 0xDEBB20E3. */
        out[4] = 0x4a;
        out[5] = 0xad;
        out[6] = 0x59;
        out[7] = 0xc4;
        out[8] = 0x00;
        out[9] = 0x47; /* manifest format 71 */
        out[10] = 0x00;
        out[11] = 0x01; /* version 1 */
        ar_store_be32(out + 12, 16u);
        return;
    }

    stored = g_hash_table_lookup(s->media,
                                 GUINT_TO_POINTER((guint)sector + 1));
    if (stored) {
        memcpy(out, stored, AR_SECTOR_SIZE);
    }
}

static void ar_prepare_ext_csd(AREsdhcState *s)
{
    memset(s->stream, 0, AR_SECTOR_SIZE);
    /* Identity fields checked alongside the CID against the firmware's
     * built-in Toshiba 004GE0 profile. */
    s->stream[0x98] = 0x01;
    s->stream[0x9D] = 0x01;
    s->stream[0x9E] = 0xD8;
    s->stream[0xDE] = 0x01;
    s->stream[0xE3] = 0x08;
    /* Firmware reads the 32-bit sector count directly from EXT_CSD + 0xD4. */
    ar_store_be32(s->stream + 0xD4, AR_EMMC_SECTORS);
    s->stream_len = AR_SECTOR_SIZE;
    s->stream_pos = 0;
}

static void ar_prepare_storage_read(AREsdhcState *s)
{
    uint32_t blocks = (s->blkattr >> 16) & 0xffffu;
    uint32_t sector = s->argument;
    size_t i;

    if (!blocks) {
        blocks = 1;
    }
    blocks = MIN(blocks, (uint32_t)(AR_STREAM_MAX / AR_SECTOR_SIZE));
    memset(s->stream, 0, AR_STREAM_MAX);
    for (i = 0; i < blocks; i++) {
        ar_virtual_sector(s, sector + i, s->stream + i * AR_SECTOR_SIZE);
    }
    s->stream_len = blocks * AR_SECTOR_SIZE;
    s->stream_pos = 0;
}

static void ar_prepare_storage_write(AREsdhcState *s)
{
    uint32_t blocks = (s->blkattr >> 16) & 0xffffu;

    if (!blocks) {
        blocks = 1;
    }
    blocks = MIN(blocks, (uint32_t)(AR_STREAM_MAX / AR_SECTOR_SIZE));
    memset(s->stream, 0, blocks * AR_SECTOR_SIZE);
    s->stream_len = blocks * AR_SECTOR_SIZE;
    s->stream_pos = 0;
    s->storage_write = true;
}

static void ar_commit_storage_write(AREsdhcState *s)
{
    size_t i;

    for (i = 0; i < s->stream_len / AR_SECTOR_SIZE; i++) {
        uint8_t *sector = g_memdup2(s->stream + i * AR_SECTOR_SIZE,
                                   AR_SECTOR_SIZE);
        g_hash_table_replace(s->media,
                             GUINT_TO_POINTER((guint)(s->argument + i) + 1),
                             sector);
    }
    s->storage_write = false;
}

static void ar_erase_storage(AREsdhcState *s)
{
    uint32_t sector;

    if (s->erase_end < s->erase_start) {
        return;
    }
    for (sector = s->erase_start; sector <= s->erase_end; sector++) {
        g_hash_table_remove(s->media,
                            GUINT_TO_POINTER((guint)sector + 1));
        if (sector == UINT32_MAX) {
            break;
        }
    }
}

static void ar_pump_edma59(void)
{
    unsigned guard = 0;

    while (guard++ < 8192) {
        uint8_t raw[4];
        uint32_t erqh;
        uint8_t ch = AR_EDMA_CH;

        physical_memory_read(AR_EDMA_ERQH, raw, sizeof(raw));
        erqh = ldl_be_p(raw);
        if (!(erqh & (1u << (AR_EDMA_CH - 32)))) {
            break;
        }
        physical_memory_write(AR_EDMA_SSRT, &ch, 1);
    }
}

static uint32_t ar_stream_read(AREsdhcState *s, unsigned size)
{
    uint32_t v = 0;
    unsigned i;

    for (i = 0; i < size; i++) {
        uint8_t b = 0;
        if (s->stream_pos < s->stream_len) {
            b = s->stream[s->stream_pos++];
        } else if (s->pio_latch) {
            b = (uint8_t)s->pio_latch;
        }
        v = (v << 8) | b;
    }
    return v;
}

static void ar_stream_write(AREsdhcState *s, uint32_t value, unsigned size)
{
    unsigned i;

    for (i = 0; i < size; i++) {
        unsigned shift = 8 * (size - 1 - i);
        if (s->stream_pos < s->stream_len) {
            s->stream[s->stream_pos++] = value >> shift;
        }
    }
}

static void ar_command(AREsdhcState *s, uint32_t value)
{
    unsigned cmd = (value >> 24) & 0x3fu;
    bool dma_data = false;
    bool pio_data = false;

    s->xfertyp = value;
    memset(s->response, 0, sizeof(s->response));
    s->storage_write = false;

    switch (cmd) {
    case AR_CMD_SEND_OP_COND:
        /* Ready + sector-addressed/high-capacity eMMC. */
        s->response[0] = 0xC0000000u;
        break;
    case AR_CMD_ALL_SEND_CID:
    case AR_CMD_SEND_CID:
        /* Toshiba 004GE0, one of the stock firmware's known eMMC IDs.
         * The eSDHC exposes the 136-bit response shifted across CMDRSP0..3. */
        s->response[1] = 0x45300000u; /* "E0" */
        s->response[2] = 0x30303447u; /* "004G" */
        s->response[3] = 0x00110000u; /* MID 0x11 */
        break;
    case AR_CMD_SEND_EXT_CSD:
        ar_prepare_ext_csd(s);
        dma_data = true;
        break;
    case AR_CMD_READ_MULTIPLE:
        ar_prepare_storage_read(s);
        dma_data = true;
        break;
    case AR_CMD_WRITE_SINGLE:
    case AR_CMD_WRITE_MULTIPLE:
        ar_prepare_storage_write(s);
        ar_pump_edma59();
        ar_commit_storage_write(s);
        s->irqstat |= AR_IRQ_TC;
        break;
    case AR_CMD_ERASE_START:
        s->erase_start = s->argument;
        break;
    case AR_CMD_ERASE_END:
        s->erase_end = s->argument;
        break;
    case AR_CMD_ERASE:
        ar_erase_storage(s);
        break;
    case AR_CMD_TUNING_READ:
        /* The startup self-test writes 0x5a with CMD19, then expects the
         * paired CMD14 read to return the complemented byte. */
        s->stream_len = 0;
        s->stream_pos = 0;
        s->pio_latch = 0xa5;
        pio_data = true;
        break;
    case AR_CMD_TUNING_WRITE:
        pio_data = true;
        break;
    default:
        if (value & AR_XFERTYP_DPSEL) {
            pio_data = true;
        }
        break;
    }

    /* Every command used by the startup driver completes successfully. */
    s->irqstat |= AR_IRQ_CC;

    if (dma_data) {
        /* Channel 59 has already been programmed/enabled before the command. */
        ar_pump_edma59();
        s->irqstat |= AR_IRQ_TC;
    } else if (pio_data) {
        /* The tuning commands are synchronous one-word PIO transfers.  Mark
         * transfer complete at command issue so the firmware's TC semaphore
         * is ready before its subsequent DATPORT access. */
        s->irqstat |= AR_IRQ_TC;
    }

    ar_esdhc_update_irq(s);
}

static uint64_t ar_esdhc_read(void *opaque, hwaddr addr, unsigned size)
{
    AREsdhcState *s = opaque;
    unsigned off = addr & 0x3fff;

    /* The current generic eDMA helper performs a 16-byte minor-loop read as
     * four sequential 32-bit MMIO accesses.  While channel 59 is enabled,
     * interpret the controller's 0x20..0x2f window as repeated DATPORT FIFO
     * reads.  Once the major loop completes D_REQ clears ERQ, so ordinary CPU
     * reads of PRSSTAT/PROCTL/SYSCTL retain their normal register meanings. */
    if (off >= AR_ESDHC_DATPORT && off < AR_ESDHC_DATPORT + 0x10 &&
        ar_edma59_enabled()) {
        return ar_stream_read(s, size);
    }

    switch (off) {
    case AR_ESDHC_BLKATTR: return s->blkattr;
    case AR_ESDHC_DSADDR: return s->argument;
    case AR_ESDHC_XFERTYP: return s->xfertyp;
    case AR_ESDHC_CMDRSP0: return s->response[0];
    case AR_ESDHC_CMDRSP1: return s->response[1];
    case AR_ESDHC_CMDRSP2: return s->response[2];
    case AR_ESDHC_CMDRSP3: return s->response[3];
    case AR_ESDHC_DATPORT: return ar_stream_read(s, size);
    case AR_ESDHC_PRSSTAT:
        return AR_PRSSTAT_DAT0 | AR_PRSSTAT_BWEN | AR_PRSSTAT_BREN |
               AR_PRSSTAT_DLSL0;
    case AR_ESDHC_PROCTL: return s->proctl;
    case AR_ESDHC_SYSCTL: return s->sysctl;
    case AR_ESDHC_IRQSTAT: return s->irqstat;
    case AR_ESDHC_IRQSTATEN: return s->irqstaten;
    case AR_ESDHC_IRQSIGEN: return s->irqsigen;
    case AR_ESDHC_WML: return s->wml;
    default: return 0;
    }
}

static void ar_esdhc_write(void *opaque, hwaddr addr,
                           uint64_t value, unsigned size)
{
    AREsdhcState *s = opaque;
    unsigned off = addr & 0x3fff;
    uint32_t v = value;

    if (off == AR_ESDHC_DATPORT) {
        if (s->storage_write) {
            ar_stream_write(s, v, size);
            return;
        }
        s->pio_latch = v;
        return;
    }

    switch (off) {
    case AR_ESDHC_BLKATTR: s->blkattr = v; break;
    case AR_ESDHC_DSADDR: s->argument = v; break;
    case AR_ESDHC_XFERTYP: ar_command(s, v); break;
    case AR_ESDHC_PROCTL: s->proctl = v; break;
    case AR_ESDHC_SYSCTL:
        /* RSTA/INITA self-clear on the real controller. */
        s->sysctl = v & ~((1u << 24) | (1u << 27));
        break;
    case AR_ESDHC_IRQSTAT:
        s->irqstat &= ~v; /* W1C */
        ar_esdhc_update_irq(s);
        break;
    case AR_ESDHC_IRQSTATEN: s->irqstaten = v; break;
    case AR_ESDHC_IRQSIGEN:
        s->irqsigen = v;
        ar_esdhc_update_irq(s);
        break;
    case AR_ESDHC_WML: s->wml = v; break;
    default: break;
    }
}

static const MemoryRegionOps ar_esdhc_ops = {
    .read = ar_esdhc_read,
    .write = ar_esdhc_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

void ar_mk2_esdhc_init(MemoryRegion *sysmem)
{
    const char *mock = g_getenv("AR_MK2_MOCK_FACTORY_STATE");

    if (ar_esdhc || !mock || !*mock || strcmp(mock, "0") == 0) {
        return;
    }

    ar_esdhc = g_new0(AREsdhcState, 1);
    ar_esdhc->media = g_hash_table_new_full(g_direct_hash, g_direct_equal,
                                            NULL, g_free);
    memory_region_init_io(&ar_esdhc->iomem, NULL, &ar_esdhc_ops, ar_esdhc,
                          "ar-mk2-esdhc-mock", AR_ESDHC_SIZE);
    memory_region_add_subregion_overlap(sysmem, AR_ESDHC_BASE,
                                        &ar_esdhc->iomem, 60);

    qemu_log_mask(LOG_UNIMP,
                  "AR-MK2: emulator-only virtual eMMC factory state enabled\n");
}
