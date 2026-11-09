/* boost_params.h — 由模块一系统设计智能体自动生成（export_handoff.py），请勿手改；改设计后重新生成 */
/* revision r1 */
#ifndef BOOST_PARAMS_H
#define BOOST_PARAMS_H

/* ---- PWM / 时序 ---- */
#define PWM_FSW_HZ          100000u
#define PWM_TIM_CLK_HZ      170000000u
#define PWM_ARR             1699u      /* 边沿对齐 */
#define PWM_CCR_MAX         1360u      /* Dmax = 0.8 */
#define CTRL_FREQ_HZ        20000u
#define CTRL_TS_S           (5e-05f)
#define TIM1_RCR            4u        /* 每 RCR+1 个 PWM 周期进一次控制中断 */

/* ---- 采样换算：物理量 = code * K ---- */
#define ADC_FULL_SCALE      4095u
#define ADC_VREF_V          (3.3f)
#define VOUT_V_PER_CODE     (0.004270593f)  /* 分压 43000/10000 */
#define IOUT_A_PER_CODE     (0.0003223443f)  /* Rs=0.05Ω × G50 */
#define VIN_V_PER_CODE      (0.001611722f)

/* ---- 设定值 ---- */
#define VOUT_SET_V          (12.0f)
#define IOUT_SET_A          (0.5f)
#define SOFTSTART_MS        20u

/* ---- PI（u=占空比 0..1；e 为物理量误差）初值，需联调确认 ---- */
#define CV_KP               (0.004871f)   /* 1/V */
#define CV_KI_TS            (0.001686f)   /* Ki*Ts, 1/V per sample */
#define CC_KP               (0.2461f)   /* 1/A */
#define CC_KI_TS            (0.04269f)   /* Ki*Ts, 1/A per sample */
#define DUTY_MIN            (0.0f)
#define DUTY_MAX            (0.8f)

/* ---- 保护 ---- */
#define OVP_SW_V            (13.2f)
#define OVP_HW_CODE         3231u    /* COMP 参考 DAC 码值，对应 13.8 V */
#define OCP_SW_A            (0.6f)
#define OCP_SW_DELAY_MS     10u
#define OCP_HW_CODE         3102u    /* 对应 1.0 A */
#define UVLO_OFF_V          (4.3f)
#define UVLO_ON_V           (4.5f)

#endif
