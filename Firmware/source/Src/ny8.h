/*
Copyright (C) 2026 divadiow
SPDX-License-Identifier: GPL-3.0-or-later

Experimental, read-only Nyquest NY8A054E support for Easy PDK Programmer Lite.
*/

#ifndef __NY8_H_
#define __NY8_H_

#include <stdbool.h>
#include <stdint.h>

#define NY8_ALLOW_VPP       0
#define NY8_EXPERIMENT_ONLY 1
#define NY8_WEAK_PULL_IO    1
#define NY8_TARGET_VDD_MV   3300u
#define NY8_DUMP_WORDS      2048u
#define NY8_DUMP_COMMAND_RX_BYTES 4u
#define NY8_DUMP_DATA_BYTES (2u*NY8_DUMP_WORDS)
#define NY8_DUMP_RX_BYTES   (NY8_DUMP_COMMAND_RX_BYTES+NY8_DUMP_DATA_BYTES)
#define NY8_DUMP_EXP5_PREFIX_BYTES 36u
#define NY8_DUMP_BLOCK_WORDS 8u
#define NY8_DUMP_BLOCK_BYTES (2u*NY8_DUMP_BLOCK_WORDS)
#define NY8_DUMP_USB_CHUNK_MAX 60u
#define NY8_INFO_START_ADDRESS 0x0005u
#define NY8_INFO_END_ADDRESS   0x0015u
#define NY8_INFO_WORDS         (NY8_INFO_END_ADDRESS-NY8_INFO_START_ADDRESS+1u)
#define NY8_INFO_PASSES        2u
#define NY8_INFO_COMMAND_RX_BYTES 4u
#define NY8_INFO_DATA_BYTES    (2u*NY8_INFO_WORDS)
#define NY8_INFO_PASS_RX_BYTES (NY8_INFO_COMMAND_RX_BYTES+NY8_INFO_DATA_BYTES)
#define NY8_INFO_RX_BYTES      (NY8_INFO_PASSES*NY8_INFO_PASS_RX_BYTES)

#if NY8_ALLOW_VPP != 0
#error "NY8EXP7 must be built with VPP hard-disabled"
#endif

#if NY8_WEAK_PULL_IO != 1
#error "NY8EXP7 must use current-limited weak-pull signalling"
#endif

#if NY8_EXPERIMENT_ONLY != 1
#error "NY8EXP7 must compile out ordinary EasyPDK target commands"
#endif

#if NY8_TARGET_VDD_MV != 3300u
#error "NY8EXP7 target VDD must remain fixed at 3300 mV"
#endif

#if NY8_DUMP_WORDS != 2048u || NY8_DUMP_DATA_BYTES != 4096u || NY8_DUMP_RX_BYTES != 4100u
#error "NY8EXP7 dump must remain fixed at 2048 words and 4100 returned transfer bytes"
#endif

#if NY8_DUMP_EXP5_PREFIX_BYTES != NY8_DUMP_COMMAND_RX_BYTES+(2u*16u) || \
    NY8_DUMP_BLOCK_BYTES != 16u || \
    (NY8_DUMP_RX_BYTES-NY8_DUMP_EXP5_PREFIX_BYTES)%NY8_DUMP_BLOCK_BYTES != 0u
#error "NY8EXP7 transfer partition must preserve the EXP5 prefix and fixed blocks"
#endif

#if NY8_DUMP_USB_CHUNK_MAX != 60u || NY8_DUMP_USB_CHUNK_MAX >= 64u
#error "NY8EXP7 USB retrieval chunks must remain below one 64-byte CDC packet"
#endif

#if NY8_INFO_WORDS != 17u || NY8_INFO_PASSES != 2u || \
    NY8_INFO_PASS_RX_BYTES != 38u || NY8_INFO_RX_BYTES != 76u
#error "NY8EXP7 information read must remain two fixed 0x05..0x15 passes"
#endif

typedef enum NY8RESULT
{
  NY8_RESULT_HANDSHAKE_FAILED = 0,
  NY8_RESULT_OK               = 1,
  NY8_RESULT_NOT_LITE         = 2,
  NY8_RESULT_VPP_NOT_OFF      = 3,
  NY8_RESULT_VDD_OUT_OF_RANGE = 4,
  NY8_RESULT_BUFFER_TOO_SMALL = 5,
  NY8_RESULT_ABORTED          = 6,
  NY8_RESULT_POWER_OFF_FAILED = 7,
  NY8_RESULT_INTERNAL_ERROR   = 8,
  NY8_RESULT_ADC_TIMEOUT      = 9,
  NY8_RESULT_CONTROL_SDO_HIGH = 10,
  NY8_RESULT_CONTROL_SDO_LOW  = 11,
  NY8_RESULT_INVALID1_SDO_HIGH = 12,
  NY8_RESULT_INVALID1_SDO_LOW  = 13,
  NY8_RESULT_CAPTURE16_COMPLETE = 15,
  NY8_RESULT_DUMP2048_COMPLETE = 16,
  NY8_RESULT_INFO_COMPLETE     = 17,
  NY8_RESULT_INFO_MISMATCH     = 18,
} NY8RESULT;

typedef struct NY8STATUS
{
  uint32_t result;
  uint32_t hw_variant;
  uint32_t target_vdd_mv;
  uint32_t active_vdd_mv;
  uint32_t active_vpp_mv;
  uint32_t off_vdd_mv;
  uint32_t off_vpp_mv;
  uint32_t capture_count;
} NY8STATUS;

void      NY8_InitPins(void);
void      NY8_DeInitPins(void);
void      NY8_PowerOff(void);
void      NY8_Abort(void);
NY8RESULT NY8_Dump2048NoVPP(NY8STATUS* status, uint8_t* raw_rx, uint32_t raw_rx_capacity);
NY8RESULT NY8_ReadInfoNoVPP(NY8STATUS* status, uint8_t* raw_rx, uint32_t raw_rx_capacity);

#endif //__NY8_H_
