/*
 * Minimal MCF5441x DSPI0/DSPI1 model for Analog Rytm MKII emulation.
 *
 * DSPI0 also exposes an optional emulator-only SPI NOR calibration profile.
 * It is deliberately disabled unless AR_MK2_MOCK_CALIBRATION is set.  This
 * models persistent factory state for QEMU only; hardware-validation paths
 * must run without the environment variable and therefore see the physical
 * unit's real calibration storage and measurements.
 */

#include "qemu/osdep.h"
#include "system/memory.h"

#define AR_DSPI0_BASE 0xFC05C000u
#define AR_DSPI1_BASE 0xFC03C000u
#define AR_DSPI_SIZE  0x4000u
#define AR_DSPI_FIFO_DEPTH 16

#define AR_DSPI_SR_EOQF   (1u << 28)
#define AR_DSPI_SR_TCF    (1u << 31)
#define AR_DSPI_SR_RFDF   (1u << 17)
#define AR_DSPI_SR_RXCTR_SHIFT 4
#define AR_DSPI_SR_RXCTR_MASK  (0xFu << AR_DSPI_SR_RXCTR_SHIFT)

/* MCF DSPI PUSHR control bits used by the firmware. */
#define AR_DSPI_PUSHR_CONT  (1u << 31)
#define AR_DSPI_PUSHR_EOQ   (1u << 27)
#define AR_DSPI_PUSHR_CTCNT (1u << 26)

/* OS 1.72 calibration record recovered from its normal validator. */
#define AR_CAL_PRIMARY_ADDR       0x00340000u
#define AR_CAL_RECORD_SIZE        110706u
#define AR_CAL_MAGIC              0x52424F57u /* "RBOW" */
#define AR_CAL_HEADER_SIZE_FIELD  12490u
#define AR_CAL_VERSION            5u
#define AR_CAL_STATUS_OFF         0x0010u
#define AR_CAL_CHECKSUM1_OFF      0x000Cu
#define AR_CAL_CHECKSUM1_DATA_OFF 0x0010u
#define AR_CAL_CHECKSUM1_LEN      0x30BAu
#define AR_CAL_TOTAL_SIZE_OFF     0x30CCu
#define AR_CAL_CHECKSUM2_OFF      0x30D0u
#define AR_CAL_CHECKSUM2_DATA_OFF 0x30D4u
#define AR_CAL_CHECKSUM2_BIAS     0x30E4u
#define AR_CAL_V5_FLAGS_OFF       0x3DD0u
#define AR_CAL_V5_FLAGS_COUNT     6u

void ar_mk2_dtim_init(MemoryRegion *sysmem);

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

    bool spi_transaction;
    uint8_t spi_command;
    uint32_t spi_address;
    unsigned spi_address_bytes;

    bool mock_calibration;
    uint8_t *mock_calibration_record;
} ARDspiState;

static ARDspiState *ar_dspi;

static void ar_put_be16(uint8_t *p, uint16_t value)
{
    p[0] = value >> 8;
    p[1] = value;
}

static void ar_put_be32(uint8_t *p, uint32_t value)
{
    p[0] = value >> 24;
    p[1] = value >> 16;
    p[2] = value >> 8;
    p[3] = value;
}

/* Exact checksum used by OS 1.72's calibration validator at 0x400F707C. */
static uint32_t ar_cal_checksum(const uint8_t *data, size_t len)
{
    uint32_t sum = 0;
    size_t i;

    for (i = 0; i < len; i++) {
        sum += ((uint32_t)data[i]) ^ (uint32_t)(i + 1);
    }
    return sum;
}

static uint8_t *ar_build_mock_calibration(void)
{
    uint8_t *record = g_malloc0(AR_CAL_RECORD_SIZE);
    uint32_t checksum;
    unsigned i;

    ar_put_be32(record + 0x00, AR_CAL_MAGIC);
    ar_put_be32(record + 0x04, AR_CAL_HEADER_SIZE_FIELD);
    ar_put_be32(record + 0x08, AR_CAL_VERSION);

    /* Status 1 is the firmware's normal-current calibration state. */
    ar_put_be16(record + AR_CAL_STATUS_OFF, 1);

    /* OS 1.72's v4->v5 migration initializes these six fields to one. */
    for (i = 0; i < AR_CAL_V5_FLAGS_COUNT; i++) {
        record[AR_CAL_V5_FLAGS_OFF + i] = 1;
    }

    ar_put_be32(record + AR_CAL_TOTAL_SIZE_OFF, AR_CAL_RECORD_SIZE);

    checksum = ar_cal_checksum(record + AR_CAL_CHECKSUM1_DATA_OFF,
                               AR_CAL_CHECKSUM1_LEN);
    ar_put_be32(record + AR_CAL_CHECKSUM1_OFF, checksum);

    checksum = ar_cal_checksum(record + AR_CAL_CHECKSUM2_DATA_OFF,
                               AR_CAL_RECORD_SIZE - AR_CAL_CHECKSUM2_BIAS);
    ar_put_be32(record + AR_CAL_CHECKSUM2_OFF, checksum);

    return record;
}

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

static uint8_t ar_mock_flash_read(const ARDspiState *s, uint32_t address)
{
    if (s->mock_calibration &&
        address >= AR_CAL_PRIMARY_ADDR &&
        address < AR_CAL_PRIMARY_ADDR + AR_CAL_RECORD_SIZE) {
        return s->mock_calibration_record[address - AR_CAL_PRIMARY_ADDR];
    }

    /* Preserve the pre-profile discovery behavior outside modeled ranges. */
    return 0;
}

static uint8_t ar_dspi_spi_exchange(ARDspiState *s, uint8_t tx)
{
    if (!s->spi_transaction) {
        s->spi_transaction = true;
        s->spi_command = tx;
        s->spi_address = 0;
        s->spi_address_bytes = 0;
        return 0;
    }

    /* Standard 0x03 READ, used by the firmware's SPI NOR abstraction. */
    if (s->index == 0 && s->spi_command == 0x03) {
        if (s->spi_address_bytes < 3) {
            s->spi_address = (s->spi_address << 8) | tx;
            s->spi_address_bytes++;
            return 0;
        }

        tx = ar_mock_flash_read(s, s->spi_address & 0x00FFFFFFu);
        s->spi_address = (s->spi_address + 1) & 0x00FFFFFFu;
        return tx;
    }

    return 0;
}

static void ar_dspi_end_spi_transaction(ARDspiState *s)
{
    s->spi_transaction = false;
    s->spi_command = 0;
    s->spi_address = 0;
    s->spi_address_bytes = 0;
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
    case 0x2c: s->sr_flags &= ~v; break;
    case 0x30: s->rser = v; break;
    case 0x34: {
        uint8_t rx;

        /* Firmware asserts CTCNT on the command word that begins a new
         * peripheral transaction (for SPI NOR READ this is 0x84020003).
         * Treat that as the authoritative frame boundary so an earlier DSPI
         * user cannot leave the attached-device parser in stale state. */
        if (v & AR_DSPI_PUSHR_CTCNT) {
            ar_dspi_end_spi_transaction(s);
        }
        rx = ar_dspi_spi_exchange(s, (uint8_t)v);
        ar_dspi_push_rx(s, rx);
        if (!(v & AR_DSPI_PUSHR_CONT) || (v & AR_DSPI_PUSHR_EOQ)) {
            ar_dspi_end_spi_transaction(s);
        }
        break;
    }
    default: break;
    }
}

static const MemoryRegionOps ar_dspi_ops = {
    .read = ar_dspi_read,
    .write = ar_dspi_write,
    .endianness = DEVICE_BIG_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void ar_mk2_dspi_init(MemoryRegion *sysmem)
{
    static const hwaddr bases[2] = { AR_DSPI0_BASE, AR_DSPI1_BASE };
    const char *mock_cal = g_getenv("AR_MK2_MOCK_CALIBRATION");
    unsigned i;

    if (ar_dspi) {
        return;
    }
    ar_dspi = g_new0(ARDspiState, 2);
    for (i = 0; i < 2; i++) {
        ARDspiState *s = &ar_dspi[i];
        s->index = i;
        if (i == 0 && mock_cal && *mock_cal && strcmp(mock_cal, "0") != 0) {
            s->mock_calibration = true;
            s->mock_calibration_record = ar_build_mock_calibration();
        }
        memory_region_init_io(&s->iomem, NULL, &ar_dspi_ops, s,
                              i ? "ar-mk2-dspi1" : "ar-mk2-dspi0",
                              AR_DSPI_SIZE);
        memory_region_add_subregion_overlap(sysmem, bases[i], &s->iomem, 40);
    }

    /* The machine already calls this constructor during topology creation;
     * initialize the higher-priority DTIM overlay in the same safe phase. */
    ar_mk2_dtim_init(sysmem);
}
