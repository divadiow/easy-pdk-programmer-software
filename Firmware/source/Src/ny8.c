/*
Copyright (C) 2026 divadiow
SPDX-License-Identifier: GPL-3.0-or-later

Experimental, read-only Nyquest NY8A054E support for Easy PDK Programmer Lite.

The fixed program-read transaction was adapted from James Wang's MIT-licensed
PixMob_IR ny8_dumper.  See THIRD_PARTY_NOTICES.md in the repository root.

This build records either one fixed 2048-word program read or two fixed
configuration/information reads.  VPP is compile-time locked off, and SCK/SDI
use only the STM32's weak internal pulls.
*/

#include "ny8.h"

#include "fpdk.h"
#include "fpdkusb.h"
#include "main.h"

#include <string.h>

// Lite r1 SO16 adapter: A3/SCK -> PB3, A4/SDO -> PB4, A0/SDI -> PB6.
#define NY8_SCK_PORT          IC_IO_PA3_CLK_GPIO_Port
#define NY8_SCK_PIN           IC_IO_PA3_CLK_Pin
#define NY8_SDO_PORT          IC_IO_PA4_GPIO_Port
#define NY8_SDO_PIN           IC_IO_PA4_Pin
#define NY8_SDI_PORT          IC_IO_PA0_UART1_TX_GPIO_Port
#define NY8_SDI_PIN           IC_IO_PA0_UART1_TX_Pin

// Lite r1 maps these nets to STM32 PB3 and PB6.  Switching input pulls rather
// than output levels limits opposed-drive current even if the target is not NY8.
#define NY8_SCK_PULL_SHIFT    (3u*2u)
#define NY8_SDI_PULL_SHIFT    (6u*2u)
#define NY8_PULL_UP           1u
#define NY8_PULL_DOWN         2u

_Static_assert(NY8_SCK_PIN == GPIO_PIN_3, "NY8 weak-pull SCK shift no longer matches pin mapping");
_Static_assert(NY8_SDI_PIN == GPIO_PIN_6, "NY8 weak-pull SDI shift no longer matches pin mapping");

// TIM2 is already running at 48 MHz with prescaler 0 in the stock firmware.
#define NY8_TICKS_WEAK_HALF   240u // 5 us allows the 25-55K internal pull to settle
#define NY8_TICKS_WEAK_SETUP  240u
#define NY8_TICKS_1_5_US      72u
#define NY8_TICKS_500_US      24000u
#define NY8_TICKS_INFO_GAP    2400u
#define NY8_TICKS_100_MS      4800000u
#define NY8_TICKS_ADC_TIMEOUT 24000000u
#define NY8_TICKS_ADC_STARTUP_TIMEOUT 96000000u

#define NY8_ADC_FRESH_WINDOWS 2u
#define NY8_ADC_STARTUP_WINDOWS 12u

#define NY8_MAX_ACTIVE_VPP_MV 250u
#define NY8_MIN_ACTIVE_VDD_MV 2800u
#define NY8_MAX_ACTIVE_VDD_MV 3800u
#define NY8_MAX_OFF_RAIL_MV   500u

static volatile bool _abort_requested = false;

static inline void _NY8_SetWeakPull(GPIO_TypeDef* port, uint32_t shift, uint32_t pull)
{
  port->PUPDR = (port->PUPDR & ~(3u<<shift)) | (pull<<shift);
}

static inline void _NY8_SckLow(void)
{
  _NY8_SetWeakPull(NY8_SCK_PORT, NY8_SCK_PULL_SHIFT, NY8_PULL_DOWN);
}

static inline void _NY8_SckHigh(void)
{
  _NY8_SetWeakPull(NY8_SCK_PORT, NY8_SCK_PULL_SHIFT, NY8_PULL_UP);
}

static inline void _NY8_SdiLow(void)
{
  _NY8_SetWeakPull(NY8_SDI_PORT, NY8_SDI_PULL_SHIFT, NY8_PULL_DOWN);
}

static inline void _NY8_SdiHigh(void)
{
  _NY8_SetWeakPull(NY8_SDI_PORT, NY8_SDI_PULL_SHIFT, NY8_PULL_UP);
}

static inline void _NY8_SetSdi(bool high)
{
  if( high )
    _NY8_SdiHigh();
  else
    _NY8_SdiLow();
}

static inline bool _NY8_GetSdo(void)
{
  return 0 != (NY8_SDO_PORT->IDR & NY8_SDO_PIN);
}

static void _NY8_DelayTicks(uint32_t ticks)
{
  uint32_t start = TIM2->CNT;
  while( (uint32_t)(TIM2->CNT-start) < ticks )
  {
  }
}

static bool _NY8_DelayTicksAbortable(uint32_t ticks)
{
  uint32_t start = TIM2->CNT;
  while( (uint32_t)(TIM2->CNT-start) < ticks )
  {
    if( _abort_requested )
      return false;
  }
  return true;
}

static bool _NY8_DelayUntilAbortable(uint32_t start, uint32_t ticks)
{
  while( (uint32_t)(TIM2->CNT-start) < ticks )
  {
    if( _abort_requested )
      return false;
  }
  return true;
}

static bool _NY8_WaitForAdcUpdates(uint32_t start_generation, bool abortable)
{
  uint32_t start_ticks = TIM2->CNT;
  while( (uint32_t)(FPDK_GetAdcSampleGeneration()-start_generation) < NY8_ADC_FRESH_WINDOWS )
  {
    if( abortable && _abort_requested )
      return false;
    if( (uint32_t)(TIM2->CNT-start_ticks) >= NY8_TICKS_ADC_TIMEOUT )
      return false;
  }
  return true;
}

static bool _NY8_WaitForAdcStartup(void)
{
  uint32_t start_ticks = TIM2->CNT;
  while( FPDK_GetAdcSampleGeneration() < NY8_ADC_STARTUP_WINDOWS )
  {
    if( _abort_requested || !FPDKUSB_IsConnected() )
      return false;
    if( (uint32_t)(TIM2->CNT-start_ticks) >= NY8_TICKS_ADC_STARTUP_TIMEOUT )
      return false;
  }
  return true;
}

void NY8_InitPins(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_PULLDOWN;
  GPIO_InitStruct.Pin = NY8_SCK_PIN;
  HAL_GPIO_Init(NY8_SCK_PORT, &GPIO_InitStruct);
  GPIO_InitStruct.Pin = NY8_SDI_PIN;
  HAL_GPIO_Init(NY8_SDI_PORT, &GPIO_InitStruct);

  GPIO_InitStruct.Pin = NY8_SDO_PIN;
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(NY8_SDO_PORT, &GPIO_InitStruct);
}

void NY8_DeInitPins(void)
{
  _NY8_SckLow();
  _NY8_SdiLow();

  GPIO_InitTypeDef GPIO_InitStruct = {0};
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_HIGH;
  GPIO_InitStruct.Pin = NY8_SCK_PIN;
  HAL_GPIO_Init(NY8_SCK_PORT, &GPIO_InitStruct);
  GPIO_InitStruct.Pin = NY8_SDO_PIN;
  HAL_GPIO_Init(NY8_SDO_PORT, &GPIO_InitStruct);
  GPIO_InitStruct.Pin = NY8_SDI_PIN;
  HAL_GPIO_Init(NY8_SDI_PORT, &GPIO_InitStruct);
}

static uint32_t _NY8_PowerOffAndGetAdcGeneration(void)
{
  uint32_t primask = __get_PRIMASK();
  __disable_irq();
  uint32_t adc_generation = FPDK_GetAdcSampleGeneration();
  _NY8_SckLow();
  _NY8_SdiLow();
  FPDK_SetVPP(0, 0);
  FPDK_SetVDD(0, 0);
  if( !primask )
    __enable_irq();
  NY8_DeInitPins();
  return adc_generation;
}

void NY8_PowerOff(void)
{
  (void)_NY8_PowerOffAndGetAdcGeneration();
}

void NY8_Abort(void)
{
  _abort_requested = true;
  NY8_PowerOff();
}

static uint8_t _NY8_TransferByteLocked(uint8_t data_out)
{
  uint8_t mask = 0x80;
  uint8_t data_in = 0;

  _NY8_SetSdi(0 != (data_out & mask));
  _NY8_DelayTicks(NY8_TICKS_WEAK_SETUP);

  do
  {
    if( _NY8_GetSdo() )
      data_in |= mask;

    _NY8_SckHigh();

    mask >>= 1;
    _NY8_SetSdi(0 != (data_out & mask));

    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
    _NY8_SckLow();
    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
  } while( mask );

  return data_in;
}

// Q-Writer uses a distinct falling-edge convention for opcode-0x60 setup and
// address framing: SCK rises first, then the current SDI bit is presented while
// SCK is high, and the target latches it on the falling edge.
static uint8_t _NY8_TransferInfoByteLocked(uint8_t data_out)
{
  uint8_t mask = 0x80;
  uint8_t data_in = 0;

  _NY8_SdiLow();
  _NY8_DelayTicks(NY8_TICKS_WEAK_SETUP);
  do
  {
    _NY8_SckHigh();
    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
    _NY8_SetSdi(0 != (data_out & mask));
    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
    _NY8_SckLow();
    if( _NY8_GetSdo() )
      data_in |= mask;
    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
    mask >>= 1;
  } while( mask );
  _NY8_SdiLow();

  return data_in;
}

// Q-Writer's opcode-0x60 receiver samples after each falling edge.  On the
// final word it raises SDI during the last high phase, emits one extra clock,
// then returns SDI low.  Keep the unmodified 16 sampled bits as evidence;
// their config-field packing is deliberately decoded by the host.
static uint16_t _NY8_ReceiveInfoWordLocked(bool end_command)
{
  uint16_t raw = 0;

  _NY8_SdiLow();
  _NY8_DelayTicks(NY8_TICKS_WEAK_SETUP);
  for( uint32_t bit=0; bit<16u; bit++ )
  {
    _NY8_SckHigh();
    if( end_command && 15u==bit )
    {
      _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
      _NY8_SdiHigh();
    }
    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
    _NY8_SckLow();
    raw = (raw<<1) | (_NY8_GetSdo() ? 1u : 0u);
    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
  }

  if( end_command )
  {
    _NY8_SckHigh();
    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
    _NY8_SckLow();
    _NY8_DelayTicks(NY8_TICKS_WEAK_HALF);
    _NY8_SdiLow();
    _NY8_DelayTicks(NY8_TICKS_INFO_GAP);
  }

  return raw;
}

static bool _NY8_InfoIdentityWordsMatch(const uint8_t* first, const uint8_t* second)
{
  static const uint8_t word_indices[] = { 0u, 10u, 11u, 12u, 16u };

  for( uint32_t i=0; i<sizeof(word_indices); i++ )
  {
    uint32_t offset = 2u*word_indices[i];
    uint16_t first_raw = ((uint16_t)first[offset+0u]<<8) | first[offset+1u];
    uint16_t second_raw = ((uint16_t)second[offset+0u]<<8) | second[offset+1u];
    uint16_t first_word = ((first_raw>>2)&0x3F80u) | ((first_raw>>1)&0x007Fu);
    uint16_t second_word = ((second_raw>>2)&0x3F80u) | ((second_raw>>1)&0x007Fu);
    if( first_word != second_word )
      return false;
  }
  return true;
}

typedef enum NY8STIMULUS
{
  NY8_STIMULUS_NONE,
  NY8_STIMULUS_EXACT,
  NY8_STIMULUS_INVALID_FIRST,
} NY8STIMULUS;

static void _NY8_SendHandshake(NY8STIMULUS stimulus)
{
  uint32_t primask = __get_PRIMASK();
  __disable_irq();
  _NY8_TransferByteLocked((NY8_STIMULUS_INVALID_FIRST == stimulus) ? 0x52 : 0x53);
  _NY8_DelayTicks(NY8_TICKS_1_5_US);
  _NY8_TransferByteLocked(0xAD);
  if( !primask )
    __enable_irq();
}

static void _NY8_InitStatus(NY8STATUS* status)
{
  memset(status, 0, sizeof(*status));
  status->result = NY8_RESULT_INTERNAL_ERROR;
  status->hw_variant = FPDK_GetHardwareVariant();
  status->target_vdd_mv = NY8_TARGET_VDD_MV;
}

static NY8RESULT _NY8_ReadAndCheckActiveRails(NY8STATUS* status)
{
  status->active_vdd_mv = FPDK_GetAdcVdd();
  status->active_vpp_mv = FPDK_GetAdcVpp();

  if( status->active_vpp_mv > NY8_MAX_ACTIVE_VPP_MV )
    return NY8_RESULT_VPP_NOT_OFF;
  if( (status->active_vdd_mv < NY8_MIN_ACTIVE_VDD_MV) ||
      (status->active_vdd_mv > NY8_MAX_ACTIVE_VDD_MV) )
    return NY8_RESULT_VDD_OUT_OF_RANGE;

  return NY8_RESULT_OK;
}

static NY8RESULT _NY8_BeginNoVPP(NY8STATUS* status, NY8STIMULUS stimulus)
{
  _NY8_InitStatus(status);
  _abort_requested = false;
  NY8_PowerOff();

  if( !FPDKUSB_IsConnected() )
    return NY8_RESULT_ABORTED;
  if( FPDK_HWVAR_LITE != FPDK_GetHardwareVariant() )
    return NY8_RESULT_NOT_LITE;
  if( !_NY8_WaitForAdcStartup() )
    return _abort_requested ? NY8_RESULT_ABORTED : NY8_RESULT_ADC_TIMEOUT;

  NY8_InitPins();

  NY8RESULT power_result = NY8_RESULT_OK;
  uint32_t adc_generation = 0;
  uint32_t primask = __get_PRIMASK();
  __disable_irq();
  if( _abort_requested || !FPDKUSB_IsConnected() )
    power_result = NY8_RESULT_ABORTED;
  else
  {
    adc_generation = FPDK_GetAdcSampleGeneration();
    if( !FPDK_SetVPP(0, 0) )
      power_result = NY8_RESULT_INTERNAL_ERROR;
    else if( !FPDK_SetVDD(NY8_TARGET_VDD_MV, 0) )
      power_result = NY8_RESULT_VDD_OUT_OF_RANGE;
  }
  if( !primask )
    __enable_irq();
  if( NY8_RESULT_OK != power_result )
    return power_result;

  if( !_NY8_DelayTicksAbortable(NY8_TICKS_500_US) )
    return NY8_RESULT_ABORTED;

  uint32_t observation_epoch = TIM2->CNT;
  if( NY8_STIMULUS_NONE != stimulus )
    _NY8_SendHandshake(stimulus);
  // The control path leaves SCK and SDI weakly pulled low and emits no clocks.

  if( !_NY8_DelayUntilAbortable(observation_epoch, NY8_TICKS_100_MS) )
    return NY8_RESULT_ABORTED;

  bool sdo_high = _NY8_GetSdo();
  if( !_NY8_WaitForAdcUpdates(adc_generation, true) )
    return _abort_requested ? NY8_RESULT_ABORTED : NY8_RESULT_ADC_TIMEOUT;

  NY8RESULT rail_result = _NY8_ReadAndCheckActiveRails(status);
  if( NY8_RESULT_OK != rail_result )
    return rail_result;
  if( NY8_STIMULUS_NONE == stimulus )
    return sdo_high ? NY8_RESULT_CONTROL_SDO_HIGH : NY8_RESULT_CONTROL_SDO_LOW;
  if( NY8_STIMULUS_INVALID_FIRST == stimulus )
    return sdo_high ? NY8_RESULT_INVALID1_SDO_HIGH : NY8_RESULT_INVALID1_SDO_LOW;

  if( sdo_high )
    return NY8_RESULT_HANDSHAKE_FAILED;

  return NY8_RESULT_OK;
}

static NY8RESULT _NY8_Finish(NY8STATUS* status, NY8RESULT result, uint32_t capture_count)
{
  uint32_t adc_generation = _NY8_PowerOffAndGetAdcGeneration();
  status->capture_count = capture_count;

  if( !_NY8_WaitForAdcUpdates(adc_generation, false) )
    result = NY8_RESULT_ADC_TIMEOUT;
  else
  {
    status->off_vdd_mv = FPDK_GetAdcVdd();
    status->off_vpp_mv = FPDK_GetAdcVpp();
    if( (status->off_vdd_mv > NY8_MAX_OFF_RAIL_MV) ||
        (status->off_vpp_mv > NY8_MAX_OFF_RAIL_MV) )
      result = NY8_RESULT_POWER_OFF_FAILED;
  }

  status->result = result;
  return result;
}

NY8RESULT NY8_Dump2048NoVPP(NY8STATUS* status, uint8_t* raw_rx, uint32_t raw_rx_capacity)
{
  if( !status )
  {
    NY8_PowerOff();
    return NY8_RESULT_INTERNAL_ERROR;
  }

  if( !raw_rx || raw_rx_capacity < NY8_DUMP_RX_BYTES )
  {
    _NY8_InitStatus(status);
    return _NY8_Finish(status, NY8_RESULT_BUFFER_TOO_SMALL, 0);
  }

  memset(raw_rx, 0xFF, NY8_DUMP_RX_BYTES);
  NY8RESULT result = _NY8_BeginNoVPP(status, NY8_STIMULUS_EXACT);
  uint32_t capture_count = 0;
  uint32_t adc_generation = FPDK_GetAdcSampleGeneration();

  if( NY8_RESULT_OK == result )
  {
    if( _abort_requested || !FPDKUSB_IsConnected() )
      result = NY8_RESULT_ABORTED;
    else if( _NY8_GetSdo() )
      result = NY8_RESULT_HANDSHAKE_FAILED;
    else
    {
      uint32_t primask = __get_PRIMASK();
      __disable_irq();
      raw_rx[0] = _NY8_TransferByteLocked(0x20);
      for( uint32_t i=1; i<NY8_DUMP_EXP5_PREFIX_BYTES; i++ )
        raw_rx[i] = _NY8_TransferByteLocked(0x00);
      capture_count = NY8_DUMP_EXP5_PREFIX_BYTES;
      if( !primask )
        __enable_irq();

      for( uint32_t block=NY8_DUMP_EXP5_PREFIX_BYTES;
           block<NY8_DUMP_RX_BYTES && NY8_RESULT_OK==result;
           block+=NY8_DUMP_BLOCK_BYTES )
      {
        if( _abort_requested || !FPDKUSB_IsConnected() )
        {
          result = NY8_RESULT_ABORTED;
          break;
        }

        primask = __get_PRIMASK();
        __disable_irq();
        for( uint32_t i=0; i<NY8_DUMP_BLOCK_BYTES; i++ )
          raw_rx[block+i] = _NY8_TransferByteLocked(0x00);
        capture_count = block+NY8_DUMP_BLOCK_BYTES;
        if( !primask )
          __enable_irq();

        uint32_t next_generation = FPDK_GetAdcSampleGeneration();
        if( next_generation != adc_generation )
        {
          adc_generation = next_generation;
          result = _NY8_ReadAndCheckActiveRails(status);
        }
      }

      if( NY8_RESULT_OK == result )
      {
        if( !_NY8_WaitForAdcUpdates(adc_generation, true) )
          result = _abort_requested ? NY8_RESULT_ABORTED : NY8_RESULT_ADC_TIMEOUT;
        else
          result = _NY8_ReadAndCheckActiveRails(status);
      }

      if( NY8_RESULT_OK == result && NY8_DUMP_RX_BYTES == capture_count )
        result = NY8_RESULT_DUMP2048_COMPLETE;
      else if( NY8_RESULT_OK == result )
        result = NY8_RESULT_INTERNAL_ERROR;
    }
  }

  return _NY8_Finish(status, result, capture_count);
}

NY8RESULT NY8_ReadInfoNoVPP(NY8STATUS* status, uint8_t* raw_rx, uint32_t raw_rx_capacity)
{
  if( !status )
  {
    NY8_PowerOff();
    return NY8_RESULT_INTERNAL_ERROR;
  }

  if( !raw_rx || raw_rx_capacity < NY8_INFO_RX_BYTES )
  {
    _NY8_InitStatus(status);
    return _NY8_Finish(status, NY8_RESULT_BUFFER_TOO_SMALL, 0);
  }

  memset(raw_rx, 0xFF, NY8_INFO_RX_BYTES);
  NY8RESULT result = _NY8_BeginNoVPP(status, NY8_STIMULUS_EXACT);
  uint32_t capture_count = 0;
  uint32_t adc_generation = FPDK_GetAdcSampleGeneration();

  for( uint32_t pass=0; pass<NY8_INFO_PASSES && NY8_RESULT_OK==result; pass++ )
  {
    if( _abort_requested || !FPDKUSB_IsConnected() )
    {
      result = NY8_RESULT_ABORTED;
      break;
    }
    if( 0u==pass && _NY8_GetSdo() )
    {
      result = NY8_RESULT_HANDSHAKE_FAILED;
      break;
    }

    uint32_t offset = pass*NY8_INFO_PASS_RX_BYTES;
    uint16_t wire_address = NY8_INFO_START_ADDRESS<<1;
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    raw_rx[offset+0u] = _NY8_TransferInfoByteLocked(0x60);
    raw_rx[offset+1u] = _NY8_TransferInfoByteLocked(0x00);
    raw_rx[offset+2u] = _NY8_TransferInfoByteLocked(wire_address>>8);
    raw_rx[offset+3u] = _NY8_TransferInfoByteLocked(wire_address);

    for( uint32_t word=0; word<NY8_INFO_WORDS; word++ )
    {
      uint16_t raw_word = _NY8_ReceiveInfoWordLocked(word==(NY8_INFO_WORDS-1u));
      uint32_t raw_offset = offset+NY8_INFO_COMMAND_RX_BYTES+2u*word;
      raw_rx[raw_offset+0u] = raw_word>>8;
      raw_rx[raw_offset+1u] = raw_word;
    }
    capture_count = offset+NY8_INFO_PASS_RX_BYTES;
    if( !primask )
      __enable_irq();

    uint32_t next_generation = FPDK_GetAdcSampleGeneration();
    if( next_generation != adc_generation )
    {
      adc_generation = next_generation;
      result = _NY8_ReadAndCheckActiveRails(status);
    }
  }

  if( NY8_RESULT_OK == result )
  {
    if( capture_count != NY8_INFO_RX_BYTES )
      result = NY8_RESULT_INTERNAL_ERROR;
    else if( !_NY8_WaitForAdcUpdates(adc_generation, true) )
      result = _abort_requested ? NY8_RESULT_ABORTED : NY8_RESULT_ADC_TIMEOUT;
    else
      result = _NY8_ReadAndCheckActiveRails(status);
  }

  if( NY8_RESULT_OK == result )
  {
    const uint8_t* first = &raw_rx[NY8_INFO_COMMAND_RX_BYTES];
    const uint8_t* second = &raw_rx[NY8_INFO_PASS_RX_BYTES+NY8_INFO_COMMAND_RX_BYTES];
    result = _NY8_InfoIdentityWordsMatch(first, second) ?
      NY8_RESULT_INFO_COMPLETE : NY8_RESULT_INFO_MISMATCH;
  }

  return _NY8_Finish(status, result, capture_count);
}
