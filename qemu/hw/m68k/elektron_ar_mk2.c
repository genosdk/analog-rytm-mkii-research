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
#include "qemu/log.h"
#include "qemu/datadir.h"
#include "target/m68k/cpu.h"
#include "hw/core/boards.h"
#include "hw/core/loader.h"
#include "system/system.h"
#include "system/address-spaces.h"
#include "system/memory.h"
#include "qemu/timer.h"

#define AR_MAIN_LOAD_ADDR    0x40000400u
#define AR_MAIN_ENTRY        0x40000870u
#define AR_SDRAM_BASE        0x40000000u
#define AR_DEFAULT_RAM_SIZE  (128 * MiB)
#define AR_SRAM_BASE         0x80000000u
#define AR_SRAM_SIZE         (64 * KiB)
#define AR_SRAM_APERTURE     0x0C000000u
#define AR_PBC1_BASE         0xE0000000u
#define AR_PBC0_BASE         0xF0000000u
#define AR_PBC_WINDOW        0x10000000u
#define AR_BOOT_STACK        0x47FFFFE0u
#define AR_FB_PTR_GLOBAL     0x4026F474u
#define AR_FB_BYTES          0x400u

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
    char *frame_out;
} ARBoardState;

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

    if (!s->frame_out) {
        return;
    }

    cpu_physical_memory_read(AR_FB_PTR_GLOBAL, pbuf, sizeof(pbuf));
    ptr = ldl_be_p(pbuf);
    if (ptr >= AR_SDRAM_BASE &&
        (uint64_t)ptr + AR_FB_BYTES <= AR_SDRAM_BASE + AR_DEFAULT_RAM_SIZE) {
        cpu_physical_memory_read(ptr, frame, sizeof(frame));
        if (!g_file_set_contents(s->frame_out, (const char *)frame,
                                 sizeof(frame), NULL)) {
            qemu_log_mask(LOG_GUEST_ERROR,
                          "AR-MK2: failed to write framebuffer file %s\n",
                          s->frame_out);
        }
    }

    timer_mod(s->frame_timer, qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 16);
}

static void ar_write_boot_argument(MachineState *machine)
{
    uint8_t *ram = memory_region_get_ram_ptr(machine->ram);
    hwaddr off = AR_BOOT_STACK - AR_SDRAM_BASE;

    /* Firmware reads a boot-provided long from incoming SP+4. Use zero first. */
    stl_be_p(ram + off + 4, 0);
}

static void elektron_ar_mk2_init(MachineState *machine)
{
    MemoryRegion *sysmem = get_system_memory();
    ARBoardState *s = g_new0(ARBoardState, 1);
    CPUM68KState *env;
    char *fn;
    int64_t loaded;

    if (machine->ram_size < AR_DEFAULT_RAM_SIZE) {
        error_report("elektron-ar-mk2 currently requires at least 128 MiB RAM");
        exit(1);
    }

    s->mmio_bytes = g_hash_table_new(g_direct_hash, g_direct_equal);
    {
        const char *out = g_getenv("AR_MK2_FRAMEBUFFER_OUT");
        if (out && *out) {
            s->frame_out = g_strdup(out);
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

    if (s->frame_out) {
        s->frame_timer = timer_new_ms(QEMU_CLOCK_VIRTUAL, ar_export_framebuffer, s);
        timer_mod(s->frame_timer, qemu_clock_get_ms(QEMU_CLOCK_VIRTUAL) + 16);
    }

    qemu_log_mask(LOG_UNIMP,
                  "AR-MK2: loaded %" PRId64 " bytes @ %08x; PC=%08x SP=%08x\n",
                  loaded, AR_MAIN_LOAD_ADDR, env->pc, env->aregs[7]);
}

static void elektron_ar_mk2_machine_init(MachineClass *mc)
{
    mc->desc = "Elektron Analog Rytm MKII research machine (incomplete)";
    mc->init = elektron_ar_mk2_init;
    mc->default_cpu_type = M68K_CPU_TYPE_NAME("cfv4e");
    mc->default_ram_size = AR_DEFAULT_RAM_SIZE;
    mc->default_ram_id = "elektron-ar-mk2.sdram";
}

DEFINE_MACHINE("elektron-ar-mk2", elektron_ar_mk2_machine_init)
