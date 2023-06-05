from cereal import car, log, messaging
from common.realtime import DT_CTRL
from common.numpy_fast import clip, interp
from common.conversions import Conversions as CV
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, create_lfahda_mfc, create_hda_mfc, \
                                             create_scc11, create_scc12, create_scc13, create_scc14, create_cpress, \
                                             create_scc42a, create_scc7d0, create_mdps12, create_fca11, create_fca12
from selfdrive.car.hyundai.values import Buttons, CarControllerParams, CAR, FEATURES
from opendbc.can.packer import CANPacker
from selfdrive.controls.lib.longcontrol import LongCtrlState
from selfdrive.car.hyundai.carstate import GearShifter
from selfdrive.controls.lib.desire_helper import LANE_CHANGE_SPEED_MIN

from selfdrive.car.hyundai.navicontrol  import NaviControl

from common.params import Params
import common.log as trace1
import common.CTime1000 as tm
import datetime
import statistics
from random import randint
from decimal import Decimal

import math

VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState
LongitudinalPlanSource = log.LongitudinalPlan.LongitudinalPlanSource
LaneChangeState = log.LateralPlan.LaneChangeState


def process_hud_alert(enabled, fingerprint, visual_alert, left_lane,
                      right_lane, left_lane_depart, right_lane_depart):
  sys_warning = (visual_alert in (VisualAlert.steerRequired, VisualAlert.ldw))

  # initialize to no line visible
  sys_state = 1
  if left_lane and right_lane or sys_warning:  # HUD alert only display when LKAS status is active
    sys_state = 3 if enabled or sys_warning else 4
  elif left_lane:
    sys_state = 5
  elif right_lane:
    sys_state = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if left_lane_depart:
    left_lane_warning = 1 if fingerprint in (CAR.GENESIS_DH, CAR.GENESIS_G90_HI, CAR.GENESIS_G80_DH, CAR.GENESIS_G70_IK) else 2
  if right_lane_depart:
    right_lane_warning = 1 if fingerprint in (CAR.GENESIS_DH, CAR.GENESIS_G90_HI, CAR.GENESIS_G80_DH, CAR.GENESIS_G70_IK) else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning

def lerp(a, b, t):
  return b * t + a * (1.0 - t)

class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.p = CarControllerParams(CP)
    self.packer = CANPacker(dbc_name)
    self.angle_limit_counter = 0
    self.cut_steer_frames = 0
    self.cut_steer = False

    self.apply_steer_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.steer_rate_limited = False
    self.lkas11_cnt = 0
    self.scc12_cnt = 0
    self.counter_init = False
    self.aq_value = 0
    self.aq_value_raw = 0

    self.resume_cnt = 0
    self.last_lead_distance = 0
    self.resume_wait_timer = 0

    self.last_resume_frame = 0
    self.accel = 0

    self.lanechange_manual_timer = 0
    self.emergency_manual_timer = 0
    self.driver_steering_torque_above = False
    self.driver_steering_torque_above_timer = 100
    
    self.mode_change_timer = 0

    self.acc_standstill_timer = 0
    self.acc_standstill = False

    self.need_brake = False
    self.need_brake_timer = 0

    self.cancel_counter = 0

    self.v_cruise_kph_auto_res = 0

    self.params = Params()
    self.mode_change_switch = int(self.params.get("CruiseStatemodeSelInit", encoding="utf8"))
    self.opkr_variablecruise = self.params.get_bool("OpkrVariableCruise")
    self.opkr_autoresume = self.params.get_bool("OpkrAutoResume")
    self.opkr_cruisegap_auto_adj = self.params.get_bool("CruiseGapAdjust")
    self.opkr_cruise_auto_res = self.params.get_bool("CruiseAutoRes")
    self.opkr_cruise_auto_res_option = int(self.params.get("AutoResOption", encoding="utf8"))
    self.opkr_cruise_auto_res_condition = int(self.params.get("AutoResCondition", encoding="utf8"))

    self.opkr_turnsteeringdisable = self.params.get_bool("OpkrTurnSteeringDisable")
    self.opkr_maxanglelimit = float(int(self.params.get("OpkrMaxAngleLimit", encoding="utf8")))
    self.ufc_mode_enabled = self.params.get_bool("UFCModeEnabled")
    self.ldws_fix = self.params.get_bool("LdwsCarFix")
    self.radar_helper_option = int(self.params.get("RadarLongHelper", encoding="utf8"))
    self.stopping_dist_adj_enabled = self.params.get_bool("StoppingDistAdj")
    self.standstill_resume_alt = self.params.get_bool("StandstillResumeAlt")
    self.auto_res_delay = int(self.params.get("AutoRESDelay", encoding="utf8")) * 100
    self.auto_res_delay_timer = 0
    self.stopped = False
    self.stoppingdist = float(Decimal(self.params.get("StoppingDist", encoding="utf8"))*Decimal('0.1'))

    self.longcontrol = False #CP.openpilotLongitudinalControl
    #self.scc_live is true because CP.radarOffCan is False
    self.scc_live = not CP.radarOffCan

    self.timer1 = tm.CTime1000("time")

    self.NC = NaviControl()

    self.dRel = 0
    self.vRel = 0
    self.yRel = 0

    self.cruise_gap_prev = 0
    self.cruise_gap_set_init = False
    self.cruise_gap_adjusting = False
    self.standstill_fault_reduce_timer = 0
    self.standstill_res_button = False
    self.standstill_res_count = int(self.params.get("RESCountatStandstill", encoding="utf8"))

    self.standstill_status = 0
    self.standstill_status_timer = 0
    self.switch_timer = 0
    self.switch_timer2 = 0
    self.auto_res_timer = 0
    self.auto_res_limit_timer = 0
    self.auto_res_limit_sec = int(self.params.get("AutoResLimitTime", encoding="utf8")) * 100
    self.auto_res_starting = False
    self.res_speed = 0
    self.res_speed_timer = 0
    self.autohold_popup_timer = 0
    self.autohold_popup_switch = False

    self.steerMax_base = int(self.params.get("SteerMaxBaseAdj", encoding="utf8"))
    self.steerDeltaUp_base = int(self.params.get("SteerDeltaUpBaseAdj", encoding="utf8"))
    self.steerDeltaDown_base = int(self.params.get("SteerDeltaDownBaseAdj", encoding="utf8"))
    self.steerMax_Max = int(self.params.get("SteerMaxAdj", encoding="utf8"))
    self.steerDeltaUp_Max = int(self.params.get("SteerDeltaUpAdj", encoding="utf8"))
    self.steerDeltaDown_Max = int(self.params.get("SteerDeltaDownAdj", encoding="utf8"))
    self.model_speed_range = [30, 100, 255]
    self.steerMax_range = [self.steerMax_Max, self.steerMax_base, self.steerMax_base]
    self.steerDeltaUp_range = [self.steerDeltaUp_Max, self.steerDeltaUp_base, self.steerDeltaUp_base]
    self.steerDeltaDown_range = [self.steerDeltaDown_Max, self.steerDeltaDown_base, self.steerDeltaDown_base]
    self.steerMax = 0
    self.steerDeltaUp = 0
    self.steerDeltaDown = 0

    self.variable_steer_max = self.params.get_bool("OpkrVariableSteerMax")
    self.variable_steer_delta = self.params.get_bool("OpkrVariableSteerDelta")
    self.osm_spdlimit_enabled = self.params.get_bool("OSMSpeedLimitEnable")
    self.stock_safety_decel_enabled = self.params.get_bool("UseStockDecelOnSS")
    self.joystick_debug_mode = self.params.get_bool("JoystickDebugMode")
    self.stopsign_enabled = self.params.get_bool("StopAtStopSign")

    self.smooth_start = False

    self.cc_timer = 0
    self.on_speed_control = False
    self.on_speed_bump_control = False
    self.curv_speed_control = False
    self.cut_in_control = False
    self.driver_scc_set_control = False
    self.vFuture = 0
    self.vFutureA = 0
    self.cruise_init = False
    self.change_accel_fast = False

    self.temp_disable_spamming = 0

    self.to_avoid_lkas_fault_enabled = self.params.get_bool("AvoidLKASFaultEnabled")
    self.to_avoid_lkas_fault_max_angle = int(self.params.get("AvoidLKASFaultMaxAngle", encoding="utf8"))
    self.to_avoid_lkas_fault_max_frame = int(self.params.get("AvoidLKASFaultMaxFrame", encoding="utf8"))
    self.enable_steer_more = self.params.get_bool("AvoidLKASFaultBeyond")
    self.no_mdps_mods = self.params.get_bool("NoSmartMDPS")

    #self.user_specific_feature = int(self.params.get("UserSpecificFeature", encoding="utf8"))

    self.gap_by_spd_on = self.params.get_bool("CruiseGapBySpdOn")
    self.gap_by_spd_spd = list(map(int, Params().get("CruiseGapBySpdSpd", encoding="utf8").split(',')))
    self.gap_by_spd_gap = list(map(int, Params().get("CruiseGapBySpdGap", encoding="utf8").split(',')))
    self.gap_by_spd_on_buffer1 = 0
    self.gap_by_spd_on_buffer2 = 0
    self.gap_by_spd_on_buffer3 = 0
    self.gap_by_spd_gap1 = False
    self.gap_by_spd_gap2 = False
    self.gap_by_spd_gap3 = False
    self.gap_by_spd_gap4 = False
    self.gap_by_spd_on_sw = False
    self.gap_by_spd_on_sw_trg = True
    self.gap_by_spd_on_sw_cnt = 0
    self.gap_by_spd_on_sw_cnt2 = 0

    self.radar_disabled_conf = self.params.get_bool("RadarDisable")
    self.prev_cruiseButton = 0
    self.gapsettingdance = 4
    self.lead_visible = False
    self.lead_debounce = 0
    self.radarDisableOverlapTimer = 0
    self.radarDisableActivated = False
    self.objdiststat = 0
    self.fca11supcnt = self.fca11inc = self.fca11alivecnt = self.fca11cnt13 = 0
    self.fca11maxcnt = 0xD

    self.steer_timer_apply_torque = 1.0
    self.DT_STEER = 0.005             # 0.01 1sec, 0.005  2sec

    self.lkas_onoff_counter = 0
    self.lkas_temp_disabled = False
    self.lkas_temp_disabled_timer = 0

    self.try_early_stop = self.params.get_bool("OPKREarlyStop")
    self.try_early_stop_retrieve = False
    self.try_early_stop_org_gap = 4.0

    self.ed_rd_diff_on = False
    self.ed_rd_diff_on_timer = 0
    self.ed_rd_diff_on_timer2 = 0

    self.vrel_delta = 0
    self.vrel_delta_prev = 0
    self.vrel_delta_timer = 0
    self.vrel_delta_timer2 = 0
    self.vrel_delta_timer3 = 0

    self.lead_distance_hist = []
    self.lead_distance_times = []
    self.lead_distance_histavg = []
    self.lead_distance_accuracy = []

    self.e2e_standstill_enable = self.params.get_bool("DepartChimeAtResume")
    self.e2e_standstill = False
    self.e2e_standstill_stat = False
    self.e2e_standstill_timer = 0
    self.e2e_standstill_timer_buf = 0

    self.str_log2 = 'MultiLateral'
    if CP.lateralTuning.which() == 'pid':
      self.str_log2 = 'T={:0.2f}/{:0.3f}/{:0.2f}/{:0.5f}'.format(CP.lateralTuning.pid.kpV[1], CP.lateralTuning.pid.kiV[1], CP.lateralTuning.pid.kdV[0], CP.lateralTuning.pid.kf)
    elif CP.lateralTuning.which() == 'indi':
      self.str_log2 = 'T={:03.1f}/{:03.1f}/{:03.1f}/{:03.1f}'.format(CP.lateralTuning.indi.innerLoopGainV[0], CP.lateralTuning.indi.outerLoopGainV[0], \
       CP.lateralTuning.indi.timeConstantV[0], CP.lateralTuning.indi.actuatorEffectivenessV[0])
    elif CP.lateralTuning.which() == 'lqr':
      self.str_log2 = 'T={:04.0f}/{:05.3f}/{:07.5f}'.format(CP.lateralTuning.lqr.scale, CP.lateralTuning.lqr.ki, CP.lateralTuning.lqr.dcGain)
    elif CP.lateralTuning.which() == 'torque':
      self.str_log2 = 'T={:0.2f}/{:0.2f}/{:0.2f}/{:0.3f}'.format(CP.lateralTuning.torque.kp, CP.lateralTuning.torque.kf, CP.lateralTuning.torque.ki, CP.lateralTuning.torque.friction)

    self.sm = messaging.SubMaster(['controlsState', 'radarState', 'longitudinalPlan'])

  def smooth_steer( self, apply_torque, CS ):
    if self.CP.smoothSteer.maxSteeringAngle and abs(CS.out.steeringAngleDeg) > self.CP.smoothSteer.maxSteeringAngle:
      if self.CP.smoothSteer.maxDriverAngleWait and CS.out.steeringPressed:
        self.steer_timer_apply_torque -= self.CP.smoothSteer.maxDriverAngleWait # 0.002 #self.DT_STEER   # 0.01 1sec, 0.005  2sec   0.002  5sec
      elif self.CP.smoothSteer.maxSteerAngleWait:
        self.steer_timer_apply_torque -= self.CP.smoothSteer.maxSteerAngleWait # 0.001  # 10 sec
    elif self.CP.smoothSteer.driverAngleWait and CS.out.steeringPressed:
      self.steer_timer_apply_torque -= self.CP.smoothSteer.driverAngleWait #0.001
    else:
      if self.steer_timer_apply_torque >= 1:
          return int(round(float(apply_torque)))
      self.steer_timer_apply_torque += self.DT_STEER

    if self.steer_timer_apply_torque < 0:
      self.steer_timer_apply_torque = 0
    elif self.steer_timer_apply_torque > 1:
      self.steer_timer_apply_torque = 1

    apply_torque *= self.steer_timer_apply_torque

    return  int(round(float(apply_torque)))

  def update(self, c, enabled, CS, frame, actuators, pcm_cancel_cmd, visual_alert,
             left_lane, right_lane, left_lane_depart, right_lane_depart, set_speed, lead_visible, v_future, v_future_a):

    self.vFuture = v_future
    self.vFutureA = v_future_a
    path_plan = self.NC.update_lateralPlan()
    if frame % 10 == 0:
      self.model_speed = path_plan.modelSpeed

    self.sm.update(0)
    self.dRel = self.sm['radarState'].leadOne.dRel #EON Lead
    self.vRel = self.sm['radarState'].leadOne.vRel #EON Lead
    self.yRel = self.sm['radarState'].leadOne.yRel #EON Lead

    if self.enable_steer_more and self.to_avoid_lkas_fault_enabled and abs(CS.out.steeringAngleDeg) > self.to_avoid_lkas_fault_max_angle*0.5 and \
     CS.out.vEgo <= 12.5 and not (0 <= self.driver_steering_torque_above_timer < 100):
      self.steerMax = self.steerMax_Max
      self.steerDeltaUp = self.steerDeltaUp_Max
      self.steerDeltaDown = self.steerDeltaDown_Max
    elif CS.out.vEgo > 8.3:
      if self.variable_steer_max:
        self.steerMax = interp(int(abs(self.model_speed)), self.model_speed_range, self.steerMax_range)
      else:
        self.steerMax = self.steerMax_base
      if self.variable_steer_delta:
        self.steerDeltaUp = interp(int(abs(self.model_speed)), self.model_speed_range, self.steerDeltaUp_range)
        self.steerDeltaDown = interp(int(abs(self.model_speed)), self.model_speed_range, self.steerDeltaDown_range)
      else:
        self.steerDeltaUp = self.steerDeltaUp_base
        self.steerDeltaDown = self.steerDeltaDown_base
    else:
      self.steerMax = self.steerMax_base
      self.steerDeltaUp = self.steerDeltaUp_base
      self.steerDeltaDown = self.steerDeltaDown_base

    self.p.STEER_MAX = self.steerMax
    self.p.STEER_DELTA_UP = self.steerDeltaUp
    self.p.STEER_DELTA_DOWN = self.steerDeltaDown

    # Steering Torque
    if self.CP.smoothSteer.method == 1:
      new_steer = actuators.steer * self.steerMax
      new_steer = self.smooth_steer( new_steer, CS )
    elif 0 <= self.driver_steering_torque_above_timer < 100:
      new_steer = int(round(actuators.steer * self.steerMax * (self.driver_steering_torque_above_timer / 100)))
    else:
      new_steer = int(round(actuators.steer * self.steerMax))
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, self.p)
    self.steer_rate_limited = new_steer != apply_steer

    if self.to_avoid_lkas_fault_enabled: # Shane and Greg's idea
      lkas_active = c.active
      if lkas_active and abs(CS.out.steeringAngleDeg) > self.to_avoid_lkas_fault_max_angle:
        self.angle_limit_counter += 1
      else:
        self.angle_limit_counter = 0

      # stop requesting torque to avoid 90 degree fault and hold torque with induced temporary fault
      # two cycles avoids race conditions every few minutes
      if self.angle_limit_counter > self.to_avoid_lkas_fault_max_frame:
        self.cut_steer = True
      elif self.cut_steer_frames > 1:
        self.cut_steer_frames = 0
        self.cut_steer = False

      cut_steer_temp = False
      if self.cut_steer:
        cut_steer_temp = True
        self.angle_limit_counter = 0
        self.cut_steer_frames += 1
    else:
      if self.joystick_debug_mode:
        lkas_active = c.active
      # disable when temp fault is active, or below LKA minimum speed
      elif self.opkr_maxanglelimit == 90:
        lkas_active = c.active and abs(CS.out.steeringAngleDeg) < self.opkr_maxanglelimit and CS.out.gearShifter == GearShifter.drive
      elif self.opkr_maxanglelimit > 90:
        str_angle_limit = interp(CS.out.vEgo * CV.MS_TO_KPH, [0, 20], [self.opkr_maxanglelimit+60, self.opkr_maxanglelimit])
        lkas_active = c.active and abs(CS.out.steeringAngleDeg) < str_angle_limit and CS.out.gearShifter == GearShifter.drive
      else:
        lkas_active = c.active and CS.out.gearShifter == GearShifter.drive
      if CS.mdps_error_cnt > self.to_avoid_lkas_fault_max_frame:
        self.cut_steer = True
      elif self.cut_steer_frames > 1:
        self.cut_steer_frames = 0
        self.cut_steer = False

      cut_steer_temp = False
      if self.cut_steer:
        cut_steer_temp = True
        self.cut_steer_frames += 1

    if (( CS.out.leftBlinker and not CS.out.rightBlinker) or ( CS.out.rightBlinker and not CS.out.leftBlinker)) and CS.out.vEgo < LANE_CHANGE_SPEED_MIN and self.opkr_turnsteeringdisable:
      self.lanechange_manual_timer = 50
    if CS.out.leftBlinker and CS.out.rightBlinker:
      self.emergency_manual_timer = 50
    if self.lanechange_manual_timer:
      lkas_active = False
    if self.lanechange_manual_timer > 0:
      self.lanechange_manual_timer -= 1
    if self.emergency_manual_timer > 0:
      self.emergency_manual_timer -= 1

    if abs(CS.out.steeringTorque) > 170 and CS.out.vEgo < LANE_CHANGE_SPEED_MIN:
      self.driver_steering_torque_above = True
    else:
      self.driver_steering_torque_above = False

    if self.driver_steering_torque_above == True:
      self.driver_steering_torque_above_timer -= 1
      if self.driver_steering_torque_above_timer <= 0:
        self.driver_steering_torque_above_timer = 0
    elif self.driver_steering_torque_above == False:
      self.driver_steering_torque_above_timer += 5
      if self.driver_steering_torque_above_timer >= 100:
        self.driver_steering_torque_above_timer = 100

    if self.no_mdps_mods and CS.out.vEgo < CS.CP.minSteerSpeed:
      lkas_active = False
    if not lkas_active:
      apply_steer = 0

    self.apply_steer_last = apply_steer

    if CS.cruise_active and CS.lead_distance > 149 and self.dRel < ((CS.out.vEgo * CV.MS_TO_KPH)+5) < 100 and \
     self.vRel*3.6 < -(CS.out.vEgo * CV.MS_TO_KPH * 0.16) and CS.out.vEgo > 7 and abs(CS.out.steeringAngleDeg) < 10 and not self.longcontrol:
      self.need_brake_timer += 1
      if self.need_brake_timer > 50:
        self.need_brake = True
    elif not CS.cruise_active and 1 < self.dRel < (CS.out.vEgo * CV.MS_TO_KPH * 0.5) < 13 and self.vRel*3.6 < -(CS.out.vEgo * CV.MS_TO_KPH * 0.6) and \
     5 < (CS.out.vEgo * CV.MS_TO_KPH) < 20 and not (CS.out.brakeLights or CS.out.brakePressed or CS.out.gasPressed): # generate an event to avoid collision when SCC is not activated at low speed.
      self.need_brake_timer += 1
      if self.need_brake_timer > 20:
        self.need_brake = True
    else:
      self.need_brake = False
      self.need_brake_timer = 0

    sys_warning, sys_state, left_lane_warning, right_lane_warning =\
      process_hud_alert(lkas_active, self.car_fingerprint, visual_alert,
                        left_lane, right_lane, left_lane_depart, right_lane_depart)

    clu11_speed = CS.clu11["CF_Clu_Vanz"]
    enabled_speed = 38 if CS.is_set_speed_in_mph else 60
    if clu11_speed > enabled_speed or not lkas_active or CS.out.gearShifter != GearShifter.drive:
      enabled_speed = clu11_speed

    if CS.cruise_active: # to toggle lkas, hold gap button for 1 sec
      if CS.cruise_buttons == 3:
        self.lkas_onoff_counter += 1
        self.gap_by_spd_on_sw = True
        self.gap_by_spd_on_sw_cnt2 = 0
        if self.lkas_onoff_counter > 100:
          self.lkas_onoff_counter = 0
          self.lkas_temp_disabled = not self.lkas_temp_disabled
          if self.lkas_temp_disabled:
            self.lkas_temp_disabled_timer = 0
          else:
            self.lkas_temp_disabled_timer = 15
      else:
        if self.lkas_temp_disabled_timer:
          self.lkas_temp_disabled_timer -= 1
        self.lkas_onoff_counter = 0
        if self.gap_by_spd_on_sw:
          self.gap_by_spd_on_sw = False
          self.gap_by_spd_on_sw_cnt += 1
          if self.gap_by_spd_on_sw_cnt > 4: #temporary disable of auto gap if you press gap button 5 times quickly.
            self.gap_by_spd_on_sw_trg = not self.gap_by_spd_on_sw_trg
            self.gap_by_spd_on_sw_cnt = 0
            self.gap_by_spd_on_sw_cnt2 = 0
        elif self.gap_by_spd_on_sw_cnt:
          self.gap_by_spd_on_sw_cnt2 += 1
          if self.gap_by_spd_on_sw_cnt2 > 20:
            self.gap_by_spd_on_sw_cnt = 0
            self.gap_by_spd_on_sw_cnt2 = 0
    else:
      self.lkas_onoff_counter = 0
      if self.lkas_temp_disabled_timer:
        self.lkas_temp_disabled_timer -= 1
      self.gap_by_spd_on_sw_cnt = 0
      self.gap_by_spd_on_sw_cnt2 = 0
      self.gap_by_spd_on_sw = False
      self.gap_by_spd_on_sw_trg = True

    can_sends = []

    if frame == 0: # initialize counts from last received count signals
      self.lkas11_cnt = CS.lkas11["CF_Lkas_MsgCount"] + 1
      self.scc12_cnt = CS.scc12["CR_VSM_Alive"] + 1 if not CS.no_radar else 0
    self.lkas11_cnt %= 0x10
    self.scc12_cnt %= 0xF

    can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active and not self.lkas_temp_disabled,
                                   cut_steer_temp, CS.lkas11, sys_warning, sys_state, enabled, left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, 0, self.ldws_fix, self.lkas11_cnt))

    if CS.CP.sccBus: # send lkas11 bus 1 or 2 if scc bus is
      can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active and not self.lkas_temp_disabled,
                                   cut_steer_temp, CS.lkas11, sys_warning, sys_state, enabled, left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, CS.CP.sccBus, self.ldws_fix, self.lkas11_cnt))
    if CS.CP.mdpsBus: # send lkas11 bus 1 if mdps is bus 1
      can_sends.append(create_lkas11(self.packer, frame, self.car_fingerprint, apply_steer, lkas_active and not self.lkas_temp_disabled,
                                   cut_steer_temp, CS.lkas11, sys_warning, sys_state, enabled, left_lane, right_lane,
                                   left_lane_warning, right_lane_warning, 1, self.ldws_fix, self.lkas11_cnt))
      if frame % 2: # send clu11 to mdps if it is not on bus 0
        can_sends.append(create_clu11(self.packer, frame, CS.clu11, Buttons.NONE, enabled_speed, CS.CP.mdpsBus))

    if CS.out.cruiseState.modeSel == 0 and self.mode_change_switch == 5:
      self.mode_change_timer = 50
      self.mode_change_switch = 0
    elif CS.out.cruiseState.modeSel == 1 and self.mode_change_switch == 0:
      self.mode_change_timer = 50
      self.mode_change_switch = 1
    elif CS.out.cruiseState.modeSel == 2 and self.mode_change_switch == 1:
      self.mode_change_timer = 50
      self.mode_change_switch = 2
    elif CS.out.cruiseState.modeSel == 3 and self.mode_change_switch == 2:
      self.mode_change_timer = 50
      self.mode_change_switch = 3
    elif CS.out.cruiseState.modeSel == 4 and self.mode_change_switch == 3:
      self.mode_change_timer = 50
      self.mode_change_switch = 4
    elif CS.out.cruiseState.modeSel == 5 and self.mode_change_switch == 4:
      self.mode_change_timer = 50
      self.mode_change_switch = 5
    if self.mode_change_timer > 0:
      self.mode_change_timer -= 1

    # gather all useful data for determining speed
    e2eX_speeds = self.sm['longitudinalPlan'].e2eX
    stoplinesp = self.sm['longitudinalPlan'].stoplineProb
    max_speed_in_mph = self.CP.vCruisekph * 0.621371
    driver_doing_speed = CS.out.brakeLights or CS.out.gasPressed

    # get biggest upcoming curve value, ignoring the curve we are currently on (so we plan ahead better)
    vcurv = 0
    curv_len = len(path_plan.curvatures)
    if curv_len > 0:
      curv_middle = math.floor((curv_len - 1)/2)
      for x in range(curv_middle, curv_len):
        acurval = abs(path_plan.curvatures[x] * 100)
        if acurval > vcurv:
          vcurv = acurval

    # lead car info
    l0prob = self.sm['radarState'].leadOne.modelProb
    l0d = self.sm['radarState'].leadOne.dRel
    l0v = self.sm['radarState'].leadOne.vRel
    lead_vdiff_mph = l0v * 2.23694

    # store distance history of lead car to merge with l0v to get a better speed relative value
    time_interval_for_distspeed = 0.5
    overall_confidence = 0
    l0v_distval_mph = 0
    if l0prob > 0.5:
      # ok, start averaging this distance value
      self.lead_distance_histavg.append(l0d)
      # if we've got enough data to average, do so into our main list
      if len(self.lead_distance_histavg) >= 20:
        # get some statistics on the data we've collected
        finalavg = statistics.fmean(self.lead_distance_histavg)
        # calculate accuracy based on variance within X meters
        finalacc = 1.0 - (statistics.pvariance(self.lead_distance_histavg) / 4)
        if finalacc < 0.0:
          finalacc = 0.0
        self.lead_distance_hist.append(finalavg)
        self.lead_distance_accuracy.append(finalacc)
        self.lead_distance_histavg.clear()
        # timestamp
        self.lead_distance_times.append(datetime.datetime.now())
        # should we remove an old entry now that we just added a new one?
        if len(self.lead_distance_times) > 2 and (self.lead_distance_times[-1] - self.lead_distance_times[1]).total_seconds() > time_interval_for_distspeed:
          self.lead_distance_hist.pop(0)
          self.lead_distance_times.pop(0)
          self.lead_distance_accuracy.pop(0)
      # do we have enough averaged data to calculate a speed?
      if len(self.lead_distance_times) > 1:
        time_diff = (self.lead_distance_times[-1] - self.lead_distance_times[0]).total_seconds()
        # if we've got enough data, calculate a speed based on our distance data
        # also get confidence based on the individual distance values compared
        if time_diff > time_interval_for_distspeed:
          l0v_distval_mph = ((self.lead_distance_hist[-1] - self.lead_distance_hist[0]) / time_diff) * 2.23694
          overall_confidence = self.lead_distance_accuracy[-1] * self.lead_distance_accuracy[0]
          # reduce confidence of large values different from model's values
          overall_confidence *= 1 - (abs(l0v_distval_mph - lead_vdiff_mph) / 15)
    else:
      # no lead, clear data
      self.lead_distance_hist.clear()
      self.lead_distance_times.clear()
      self.lead_distance_histavg.clear()
    
    # if we got a distspeed value, mix it with l0v based on overall confidence
    # otherwise, just use the model l0v
    if overall_confidence > 0:
      lead_vdiff_mph = lerp(lead_vdiff_mph, l0v_distval_mph, overall_confidence * 0.5)

    # start with our picked max speed
    desired_speed = max_speed_in_mph

    # make an adjustment based on e2eX (<100 usually means something amiss)
    e2eX_speed = 0
    if len(e2eX_speeds) > 0:
      e2eX_speed = e2eX_speeds[0]

    e2adj = e2eX_speed / 100
    if e2adj < 1:
      desired_speed *= e2adj

    # if we are apporaching a turn, slow down in preparation
    vcurv_adj = 3.5 / (vcurv + 3.5)
    desired_speed *= vcurv_adj

    # is there a lead?
    if l0prob > 0.5 and clu11_speed > 5:
      # amplify large lead car speed differences a bit so we react faster
      lead_vdiff_mph *= ((abs(lead_vdiff_mph) * 0.03) ** 1.2) + 1
      # calculate an estimate of the lead car's speed for purposes of setting our speed
      lead_speed = clu11_speed + lead_vdiff_mph
      # calculate lead car time
      speed_in_ms = clu11_speed * 0.44704
      lead_time = l0d / speed_in_ms
      # caculate a target lead car time, which is generally 3 seconds unless we are driving fast
      # then we need to be a little closer to keep car within good visible range
      # and prevent big gaps where cars always are cutting in
      target_time = 3-((clu11_speed/90)**3)
      # do not go under a certain lead car time for safety
      if target_time < 2.3:
        target_time = 2.3
      # calculate the difference of our current lead time and desired lead time
      lead_time_ideal_offset = lead_time - target_time
      # depending on slowing down or speeding up, scale
      if lead_time_ideal_offset < 0:
        lead_time_ideal_offset = -(-lead_time_ideal_offset * 3.5) ** 1.4 # exponentially slow down if getting closer and closer
      else:
        lead_time_ideal_offset *= 3 # boost to catch up to car infront if far away
      # calculate the final max speed we should be going based on lead car
      max_lead_adj = lead_speed + lead_time_ideal_offset
      # cap our desired_speed to this final max speed
      if desired_speed > max_lead_adj:
        desired_speed = max_lead_adj

    # about to hit a stop sign and we are going slow enough to handle it
    if stoplinesp > 0.7 and clu11_speed < 45:
      desired_speed = 0

    # what is our difference between desired speed and target speed?
    speed_diff = desired_speed - clu11_speed

    # apply a spam overpress to hurry up cruise control
    desired_speed += speed_diff * 0.6

    # if we are going much faster than we want, disable cruise to trigger more intense regen braking
    if clu11_speed > desired_speed * 1.7:
      desired_speed = 0

    # sanity checks
    if desired_speed > max_speed_in_mph:
      desired_speed = max_speed_in_mph
    if desired_speed < 0:
      desired_speed = 0

    # if we recently pressed a cruise button, don't spam more to prevent errors for a little bit
    # also take a break if we hit the gas/brake
    if CS.cruise_buttons != 0: # little longer when pressing buttons to prevent dash blips
      self.temp_disable_spamming = 6
    elif driver_doing_speed and self.temp_disable_spamming < 3: # little bit quicker spamming after gas/brake
      self.temp_disable_spamming = 3

    # count down self spamming timer
    if self.temp_disable_spamming > 0:
      self.temp_disable_spamming -= 1

    # print debug data
    trace1.printf1("vC>" + "{:.2f}".format(vcurv) + " DS>" + "{:.2f}".format(desired_speed) + ", CCr>" + "{:.2f}".format(CS.current_cruise_speed) + ", StP>" + "{:.2f}".format(stoplinesp) + ", DSpd>" + "{:.2f}".format(l0v_distval_mph) + ", DSpM>" + "{:.2f}".format(lead_vdiff_mph) + ", Conf>" + "{:.2f}".format(overall_confidence))

    cruise_difference = abs(CS.current_cruise_speed - desired_speed)
    cruise_difference_max = round(cruise_difference) # how many presses to do in bulk?
    if cruise_difference_max > 4:
      cruise_difference_max = 4 # do a max of presses at a time

    # ok, apply cruise control button spamming to match desired speed, if we have cruise on and we are not taking a break
    # also dont press buttons if the driver is hitting the gas or brake
    if cruise_difference >= 0.666 and CS.current_cruise_speed >= 20 and self.temp_disable_spamming <= 0:
      if desired_speed < 20:
        can_sends.append(create_clu11(self.packer, frame, CS.clu11, Buttons.CANCEL)) #disable cruise to come to a stop      
        self.temp_disable_spamming = 5 # we disabled cruise, don't spam more cancels
      elif CS.current_cruise_speed > desired_speed:
        for x in range(cruise_difference_max):
          can_sends.append(create_cpress(self.packer, CS.clu11, Buttons.SET_DECEL)) #slow cruise
        self.temp_disable_spamming = 3 # take a break
      elif CS.current_cruise_speed < desired_speed:
        for x in range(cruise_difference_max):
          can_sends.append(create_cpress(self.packer, CS.clu11, Buttons.RES_ACCEL)) #speed cruise
        self.temp_disable_spamming = 3 # take a break

    if CS.out.brakeLights and CS.out.vEgo == 0 and not CS.out.cruiseState.standstill:
      self.standstill_status_timer += 1
      if self.standstill_status_timer > 200:
        self.standstill_status = 1
        self.standstill_status_timer = 0
    if self.standstill_status == 1 and CS.out.vEgo > 1:
      self.standstill_status = 0
      self.standstill_fault_reduce_timer = 0
      self.last_resume_frame = frame
      self.res_switch_timer = 0
      self.resume_cnt = 0

    if CS.out.vEgo <= 1:
      long_control_state = self.sm['controlsState'].longControlState
      if long_control_state == LongCtrlState.stopping and CS.out.vEgo < 0.1 and not CS.out.gasPressed:
        self.acc_standstill_timer += 1
        if self.acc_standstill_timer >= 200:
          self.acc_standstill_timer = 200
          self.acc_standstill = True
      else:
        self.acc_standstill_timer = 0
        self.acc_standstill = False
    elif CS.out.gasPressed or CS.out.vEgo > 1:
      self.acc_standstill = False
      self.acc_standstill_timer = 0      
    else:
      self.acc_standstill = False
      self.acc_standstill_timer = 0

    if CS.CP.mdpsBus: # send mdps12 to LKAS to prevent LKAS error
      can_sends.append(create_mdps12(self.packer, frame, CS.mdps12))

    #str_log2 = 'MDPS={}  LKAS={}  LEAD={}  AQ={:+04.2f}  VF={:03.0f}/{:03.0f}  CG={:1.0f}  FR={:03.0f}'.format(
    #   CS.out.steerFaultTemporary, CS.lkas_button_on, 0 < CS.lead_distance < 149, self.aq_value if self.longcontrol else CS.scc12["aReqValue"], v_future, v_future_a, CS.cruiseGapSet, self.timer1.sampleTime()) 
    #trace1.printf2( '{}'.format( str_log2 ) )

    # str_log3 = 'V/D/R/A/M/G={:.1f}/{:.1f}/{:.1f}/{:.2f}/{:.1f}/{:1.0f}'.format(CS.clu_Vanz, CS.lead_distance, CS.lead_objspd, CS.scc12["aReqValue"], self.stoppingdist, CS.cruiseGapSet)
    # trace1.printf3('{}'.format(str_log3))

    self.cc_timer += 1
    if self.cc_timer > 100:
      self.cc_timer = 0
      # self.radar_helper_option = int(self.params.get("RadarLongHelper", encoding="utf8"))
      # self.stopping_dist_adj_enabled = self.params.get_bool("StoppingDistAdj")
      # self.standstill_res_count = int(self.params.get("RESCountatStandstill", encoding="utf8"))
      # self.opkr_cruisegap_auto_adj = self.params.get_bool("CruiseGapAdjust")
      # self.to_avoid_lkas_fault_enabled = self.params.get_bool("AvoidLKASFaultEnabled")
      # self.to_avoid_lkas_fault_max_angle = int(self.params.get("AvoidLKASFaultMaxAngle", encoding="utf8"))
      # self.to_avoid_lkas_fault_max_frame = int(self.params.get("AvoidLKASFaultMaxFrame", encoding="utf8"))
      # self.e2e_long_enabled = self.params.get_bool("E2ELong")
      # self.stopsign_enabled = self.params.get_bool("StopAtStopSign")
      # self.gap_by_spd_on = self.params.get_bool("CruiseGapBySpdOn")
      if self.params.get_bool("OpkrLiveTunePanelEnable"):
        if CS.CP.lateralTuning.which() == 'pid':
          self.str_log2 = 'T={:0.2f}/{:0.3f}/{:0.1f}/{:0.5f}'.format(float(Decimal(self.params.get("PidKp", encoding="utf8"))*Decimal('0.01')), \
           float(Decimal(self.params.get("PidKi", encoding="utf8"))*Decimal('0.001')), float(Decimal(self.params.get("PidKd", encoding="utf8"))*Decimal('0.01')), \
           float(Decimal(self.params.get("PidKf", encoding="utf8"))*Decimal('0.00001')))
        elif CS.CP.lateralTuning.which() == 'indi':
          self.str_log2 = 'T={:03.1f}/{:03.1f}/{:03.1f}/{:03.1f}'.format(float(Decimal(self.params.get("InnerLoopGain", encoding="utf8"))*Decimal('0.1')), \
           float(Decimal(self.params.get("OuterLoopGain", encoding="utf8"))*Decimal('0.1')), float(Decimal(self.params.get("TimeConstant", encoding="utf8"))*Decimal('0.1')), \
           float(Decimal(self.params.get("ActuatorEffectiveness", encoding="utf8"))*Decimal('0.1')))
        elif CS.CP.lateralTuning.which() == 'lqr':
          self.str_log2 = 'T={:04.0f}/{:05.3f}/{:07.5f}'.format(float(Decimal(self.params.get("Scale", encoding="utf8"))*Decimal('1.0')), \
           float(Decimal(self.params.get("LqrKi", encoding="utf8"))*Decimal('0.001')), float(Decimal(self.params.get("DcGain", encoding="utf8"))*Decimal('0.00001')))
        elif CS.CP.lateralTuning.which() == 'torque':
          max_lat_accel = float(Decimal(self.params.get("TorqueMaxLatAccel", encoding="utf8"))*Decimal('0.1'))
          self.str_log2 = 'T={:0.2f}/{:0.2f}/{:0.2f}/{:0.3f}'.format(float(Decimal(self.params.get("TorqueKp", encoding="utf8"))*Decimal('0.1'))/max_lat_accel, \
           float(Decimal(self.params.get("TorqueKf", encoding="utf8"))*Decimal('0.1'))/max_lat_accel, float(Decimal(self.params.get("TorqueKi", encoding="utf8"))*Decimal('0.1'))/max_lat_accel, \
           float(Decimal(self.params.get("TorqueFriction", encoding="utf8")) * Decimal('0.001')))

    # 20 Hz LFA MFA message
    if frame % 5 == 0 and self.car_fingerprint in FEATURES["send_lfahda_mfa"]:
      can_sends.append(create_lfahda_mfc(self.packer, lkas_active))

    elif frame % 5 == 0 and self.car_fingerprint in FEATURES["send_hda_mfa"]:
      can_sends.append(create_hda_mfc(self.packer, CS, enabled, left_lane, right_lane))

    new_actuators = actuators.copy()
    new_actuators.steer = apply_steer / self.p.STEER_MAX
    new_actuators.accel = self.accel
    safetycam_speed = 0 #self.NC.safetycam_speed

    self.lkas11_cnt += 1

    return new_actuators, can_sends, safetycam_speed, self.lkas_temp_disabled, (self.gap_by_spd_on_sw_trg and self.gap_by_spd_on)
