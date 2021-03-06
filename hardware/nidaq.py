from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
from MicroManagerControl import MicroManagerControl
from data_structures import MMSettings
import nidaqmx
import nidaqmx.stream_writers
import numpy as np
import copy

import time
from event_threadQ import EventThread
from gui.GUIWidgets import SettingsView
from hardware.FilterFlipper import Flippers


class NIDAQ(QObject):

    new_ni_settings = pyqtSignal(MMSettings)

    def __init__(self, event_thread: EventThread, mm_interface: MicroManagerControl):
        super().__init__()
        self.event_thread = event_thread
        self.core = self.event_thread.bridge.get_core()
        self.mm_interface = mm_interface

        #Get the EDA setting to only do things when EDA is off, otherwise the daq_actuator is active
        eda = self.core.get_property('EDA', "Label")
        self.eda = False if eda == "Off" else True

        settings = self.event_thread.bridge.get_studio().acquisitions().get_acquisition_settings()
        self.settings = MMSettings(settings)

        self.system = nidaqmx.system.System.local()

        self.sampling_rate = 500
        self.update_settings(self.settings)

        self.galvo = Galvo(self)
        self.stage = Stage(self)
        self.camera = Camera(self)
        self.aotf = AOTF(self)
        self.brightfield_control = Brightfield(self)

        self.acq = Acquisition(self, self.settings)
        self.live = LiveMode(self)

        self.task = None

        self.acq.set_z_position.connect(self.mm_interface.set_z_position)

        self.event_thread.live_mode_event.connect(self.start_live)
        self.event_thread.settings_event.connect(self.power_settings)
        self.event_thread.settings_event.connect(self.live.channel_setting)

        self.event_thread.acquisition_started_event.connect(self.run_acquisition_task)
        self.event_thread.acquisition_ended_event.connect(self.acq_done)
        self.event_thread.mda_settings_event.connect(self.new_settings)


    def init_task(self):
        try: self.task.close()
        except: print("Task close failed")
        self.task = nidaqmx.Task()
        self.task.ao_channels.add_ao_voltage_chan('Dev1/ao0') # galvo channel
        self.task.ao_channels.add_ao_voltage_chan('Dev1/ao1') # z stage
        self.task.ao_channels.add_ao_voltage_chan('Dev1/ao2') # camera channel
        self.task.ao_channels.add_ao_voltage_chan('Dev1/ao3') # aotf blanking channel
        self.task.ao_channels.add_ao_voltage_chan('Dev1/ao4') # aotf 488 channel
        self.task.ao_channels.add_ao_voltage_chan('Dev1/ao5') # aotf 561 channel

    def update_settings(self, new_settings):
        try:
            self.cycle_time = new_settings.channels['488']['exposure']
        except KeyError:
            self.cycle_time = 100

        self.sweeps_per_frame = new_settings.sweeps_per_frame
        self.frame_rate = 1/(self.cycle_time*self.sweeps_per_frame/1000)
        self.smpl_rate = round(self.sampling_rate*self.frame_rate*self.sweeps_per_frame*self.sweeps_per_frame)
        self.n_points = self.sampling_rate*self.sweeps_per_frame
        #settings for all pulses:
        self.duty_cycle = 10/self.n_points
        self.settings = new_settings
        print('NI settings set')

    @pyqtSlot(MMSettings)
    def new_settings(self, new_settings: MMSettings):
        self.settings = new_settings
        self.update_settings(new_settings)
        self.acq.update_settings(new_settings)
        self.live.update_settings(new_settings)
        print('NEW SETTINGS SET')

    @pyqtSlot(str, str, str)
    def power_settings(self, device, prop, value):
        if device == "488_AOTF" and prop == r"Power (% of max)":
            self.aotf.power_488 = float(value)
        elif device == "561_AOTF" and prop == r"Power (% of max)":
            self.aotf.power_561 = float(value)
        elif device == "exposure":
            self.settings.channels['488']['exposure'] = float(value)
            self.update_settings(self.settings)
        elif device == 'PrimeB_Camera' and prop == "TriggerMode":
            print(value)
            brightfield = True if value == "Internal Trigger" else False
            self.brightfield_control.toggle_flippers(brightfield)
        elif device == "EDA" and prop == "Label":
            eda = self.core.get_property('EDA', "Label")
            self.eda = False if eda == "Off" else True
            # Close the task if EDA is going to take over
            if self.eda:
                try:
                    self.task.close()
                except AttributeError:
                    print("No task defined yet.")
                self.event_thread.acquisition_started_event.disconnect(self.run_acquisition_task)
                self.event_thread.acquisition_ended_event.disconnect(self.acq_done)
                self.event_thread.mda_settings_event.disconnect(self.new_settings)
            else:
                self.update_settings(self.settings)
                self.event_thread.acquisition_started_event.connect(self.run_acquisition_task)
                self.event_thread.acquisition_ended_event.connect(self.acq_done)
                self.event_thread.mda_settings_event.connect(self.new_settings)

        if device in ["561_AOTF", "488_AOTF", 'exposure']:
            self.live.make_daq_data()


    @pyqtSlot(object)
    def run_acquisition_task(self, _):
        if not self.eda:
            self.event_thread.mda_settings_event.disconnect(self.new_settings)
            time.sleep(0.5)
            self.acq.run_acquisition()

    @pyqtSlot(object)
    def acq_done(self, _):
        self.event_thread.mda_settings_event.connect(self.new_settings)
        self.acq.set_z_position.emit(self.acq.orig_z_position)
        self.event_thread.mda_settings_event.connect(self.new_settings)
        time.sleep(1)
        self.init_task()
        stop_data = np.asarray([[self.galvo.parking_voltage, 0, 0, 0, 0, 0]]).astype(np.float64).transpose()
        self.task.write(stop_data)

    @pyqtSlot(bool)
    def start_live(self, live_is_on):
        self.live.toggle(live_is_on)

    def generate_one_timepoint(self, live_channel: int = None, z_inverse: bool = False):
        if live_channel == "LED":
            timepoint = np.ndarray((6,1))
            return timepoint
        print("one timepoint post_delay", self.settings.post_delay)

        if not self.settings.use_channels or live_channel is not None:
            old_post_delay = self.settings.post_delay
            self.settings.post_delay = 0.03
            galvo = self.galvo.one_frame(self.settings)
            camera = self.camera.one_frame(self.settings)
            channel_name = '488' if live_channel is None else live_channel
            stage = self.stage.one_frame(self.settings, 0)
            aotf = self.aotf.one_frame(self.settings, self.settings.channels[channel_name])
            timepoint = np.vstack((galvo, stage, camera, aotf))
            self.settings.post_delay = old_post_delay
        else:
            galvo = self.galvo.one_frame(self.settings)
            camera = self.camera.one_frame(self.settings)
            if self.settings.acq_order_mode == 1:
                timepoint = self.slices_then_channels(galvo, camera)
            elif self.settings.acq_order_mode == 0:
                timepoint = self.channels_then_slices(galvo, camera, z_inverse)
        return timepoint

    def get_slices(self):
        iter_slices = copy.deepcopy(self.settings.slices)
        iter_slices_rev = copy.deepcopy(iter_slices)
        iter_slices_rev.reverse()
        return iter_slices, iter_slices_rev

    def channels_then_slices(self, galvo, camera, z_inverse):
        iter_slices, iter_slices_rev = self.get_slices()

        slices_data = []
        slices = iter_slices if not z_inverse else iter_slices_rev
        for sli in slices:
            channels_data = []
            for channel in self.settings.channels.values():
                if channel['use']:
                    aotf = self.aotf.one_frame(self.settings, channel)
                    offset = sli - self.settings.slices[0]
                    stage = self.stage.one_frame(self.settings, offset)
                    data = np.vstack((galvo, stage, camera, aotf))
                    channels_data.append(data)
            data = np.hstack(channels_data)
            slices_data.append(data)
        return np.hstack(slices_data)

    def slices_then_channels(self, galvo, camera):
        iter_slices, iter_slices_rev = self.get_slices()
        z_iter = 0
        channels_data = []
        for channel in self.settings.channels.values():
            if channel['use']:
                slices_data = []
                slices = iter_slices if not np.mod(z_iter, 2) else iter_slices_rev
                for sli in slices:
                    aotf = self.aotf.one_frame(self.settings, channel)
                    offset = sli - self.settings.slices[0]
                    stage = self.stage.one_frame(self.settings, offset)
                    data = np.vstack((galvo, stage, camera, aotf))
                    slices_data.append(data)
                z_iter += 1
                data = np.hstack(slices_data)
                channels_data.append(data)
        return np.hstack(channels_data)

class LiveMode(QObject):
    def __init__(self, ni:NIDAQ):
        super().__init__()
        self.ni = ni
        core = self.ni.event_thread.bridge.get_core()
        self.channel_name= core.get_property('DPseudoChannel', "Label")
        self.ready = self.make_daq_data()
        self.stop = False
        self.brightfield = core.get_property('PrimeB_Camera', "TriggerMode")
        self.brightfield = (self.brightfield == "Internal Trigger")
        self.ni.brightfield_control.flippers.brightfield(self.brightfield)

    @pyqtSlot(str, str, str)
    def channel_setting(self, device, prop, value):
        if device == "DPseudoChannel" and prop == "Label":
            self.channel_name = value
            self.make_daq_data()
        if device == "PrimeB_Camera" and prop == "TriggerMode":
            self.brightfield = (value == "Internal Trigger")

    def update_settings(self, new_settings):
        self.ni.init_task()
        self.ni.task.timing.cfg_samp_clk_timing(rate=self.ni.smpl_rate,
                                             sample_mode=nidaqmx.constants.AcquisitionType.CONTINUOUS,
                                             samps_per_chan=self.daq_data.shape[1])
        self.ni.task.out_stream.regen_mode = nidaqmx.constants.RegenerationMode.DONT_ALLOW_REGENERATION
        self.ni.stream = nidaqmx.stream_writers.AnalogMultiChannelWriter(self.ni.task.out_stream,
                                                                         auto_start=False)
        self.ni.stream.write_many_sample(self.daq_data)
        self.ni.task.register_every_n_samples_transferred_from_buffer_event(self.daq_data.shape[1],
                                                                            self.get_new_data)

    def get_new_data(self, task_handle, every_n_samples_event_type, number_of_samples, callback_data):
        if self.stop:
            self.ni.task.stop()
            self.send_stop_data()
        else:
            self.ni.stream.write_many_sample(self.daq_data)
        return 0

    def make_daq_data(self):
        try:
            timepoint = self.ni.generate_one_timepoint(live_channel = self.channel_name)
        except KeyError:
            print("WARNING: are there channels in the MDA window?")
            return False
        no_frames = np.max([1, round(200/self.ni.cycle_time)])
        print("N Frames ", no_frames)
        self.daq_data = np.tile(timepoint, no_frames)
        self.stop_data = np.asarray(
                [[self.ni.galvo.parking_voltage, 0, 0, 0, 0, 0]]).astype(np.float64).transpose()
        print(self.daq_data.shape[1])
        return True

    def send_stop_data(self):
        self.ni.init_task()
        self.ni.task.write(self.stop_data)

    def toggle(self, live_is_on):

        if self.brightfield:
            self.ni.brightfield_control.led(live_is_on, 0.3)
            return

        if live_is_on:
            if not self.ready:
                core = self.ni.event_thread.bridge.get_core()
                self.channel_name = core.get_property('DPseudoChannel', "Label")
                self.ready = self.make_daq_data()
            self.stop = False
            self.update_settings(self.ni.settings)
            self.ni.task.start()
            print("STARTED", time.perf_counter())
        else:
            self.stop = True


class Acquisition(QObject):
    set_z_position = pyqtSignal(float)
    def __init__(self, ni:NIDAQ, settings: MMSettings):
        super().__init__()
        self.settings = settings
        self.ni = ni
        self.daq_data = None
        self.ready = self.make_daq_data()

    def update_settings(self, new_settings):
        self.ready = False
        self.settings = new_settings
        self.make_daq_data()
        self.ni.init_task()
        self.ni.task.timing.cfg_samp_clk_timing(rate=self.ni.smpl_rate,
                                sample_mode=nidaqmx.constants.AcquisitionType.FINITE,
                                samps_per_chan=self.daq_data.shape[1])
        self.ni.stream = nidaqmx.stream_writers.AnalogMultiChannelWriter(self.ni.task.out_stream,
                                                                         auto_start=False)
        print('Stream length ', self.daq_data.shape[1])
        self.ready = True

    def make_daq_data(self):
        try:
            timepoint = self.ni.generate_one_timepoint()
        except ValueError:
            print("WARNING: Are the channels in the MDA pannel?")
            return False
        timepoint = self.add_interval(timepoint)
        # Make zstage go up/down over two timepoints
        if self.settings.acq_order_mode == 0:
            timepoint_inverse = self.ni.generate_one_timepoint(z_inverse=True)
            timepoint_inverse = self.add_interval(timepoint_inverse)
            double_timepoint = np.hstack([timepoint, timepoint_inverse])
            self.daq_data = np.tile(double_timepoint, int(np.floor(self.settings.timepoints/2)))
            if self.settings.timepoints % 2 == 1:
                self.daq_data = np.hstack([self.daq_data, timepoint])
        else:
            self.daq_data = np.tile(timepoint, self.settings.timepoints)
        return True

    def add_interval(self, timepoint):
        if (self.ni.smpl_rate*self.settings.interval_ms/1000 <= timepoint.shape[1] and
            self.settings.interval_ms > 0):
            print('Error: interval time shorter than time required to acquire single timepoint.')
            self.settings.interval_ms = 0

        if self.settings.interval_ms > 0:
            missing_samples = round(self.ni.smpl_rate * self.settings.interval_ms/1000-timepoint.shape[1])
            galvo = np.ones(missing_samples) * self.ni.galvo.parking_voltage
            rest = np.zeros((timepoint.shape[0] - 1, missing_samples))
            delay = np.vstack([galvo, rest])
            timepoint = np.hstack([timepoint, delay])
        print("INTERVAL: ", self.settings.interval_ms)
        return timepoint

    def run_acquisition(self):
        self.update_settings(self.settings)
        self.orig_z_position = self.ni.core.get_position()
        if self.settings.use_slices:
            self.set_z_position.emit(self.settings.slices[0])
            time.sleep(0.1)
        print("WRITING, ", self.daq_data.shape)
        written = self.ni.stream.write_many_sample(self.daq_data, timeout=20)
        time.sleep(0.5)
        self.ni.task.start()
        print('================== Data written        ', written)


def make_pulse(ni, start, end, offset):
    up = np.ones(round(ni.duty_cycle*ni.n_points))*start
    down = np.ones(ni.n_points-round(ni.duty_cycle*ni.n_points))*end
    pulse = np.concatenate((up,down)) + offset
    return pulse


class Galvo:
    def __init__(self, ni: NIDAQ):
        self.ni = ni
        self.offset_0= -0.15
        self.amp_0 = 0.75
        self.parking_voltage = -3

    def one_frame(self, settings):
        self.n_points = self.ni.sampling_rate*settings.sweeps_per_frame
        down1 = np.linspace(0,-self.amp_0,round(self.n_points/(4*settings.sweeps_per_frame)))
        up = np.linspace(-self.amp_0,self.amp_0,round(self.n_points/(2*settings.sweeps_per_frame)))
        down2 = np.linspace(self.amp_0,0,round(self.n_points/settings.sweeps_per_frame) -
                            round(self.n_points/(4*settings.sweeps_per_frame)) -
                            round(self.n_points/(2*settings.sweeps_per_frame)))
        galvo_frame = np.concatenate((down1, up, down2))
        galvo_frame = np.tile(galvo_frame, settings.sweeps_per_frame)
        galvo_frame = galvo_frame + self.offset_0
        galvo_frame = galvo_frame[0:self.n_points]
        galvo_frame = self.add_delays(galvo_frame, settings)
        return galvo_frame

    def add_delays(self, frame, settings):
        if settings.post_delay > 0:
            delay = np.ones(round(self.ni.smpl_rate * settings.post_delay)) * self.parking_voltage
            frame = np.hstack([frame, delay])

        if settings.pre_delay > 0:
            delay = np.ones(round(self.ni.smpl_rate * settings.pre_delay)) * self.parking_voltage
            frame = np.hstack([delay, frame])

        return frame


class Stage:
    def __init__(self, ni: NIDAQ):
        self.ni = ni
        self.pulse_voltage = 5
        self.calibration = 202.161
        self.max_v = 10

    def one_frame(self, settings, height_offset):
        height_offset = self.convert_z(height_offset)
        stage_frame = make_pulse(self.ni, height_offset, height_offset, 0)
        stage_frame = self.add_delays(stage_frame, settings)
        return stage_frame

    def convert_z(self, z_um):
        return (z_um/self.calibration) * self.max_v

    def add_delays(self, frame, settings):
        if settings.post_delay > 0:
            delay = np.ones(round(self.ni.smpl_rate * settings.post_delay))* frame[-1]
            frame = np.hstack([frame, delay])

        if settings.pre_delay > 0:
            delay = np.ones(round(self.ni.smpl_rate * settings.pre_delay))* frame[-1]
            frame = np.hstack([delay, frame])

        return frame


class Camera:
    def __init__(self, ni: NIDAQ):
        self.ni = ni
        self.pulse_voltage = 5

    def one_frame(self, settings):
        camera_frame = make_pulse(self.ni, 5, 0, 0)
        camera_frame = self.add_delays(camera_frame, settings)
        return camera_frame

    def add_delays(self, frame, settings):
        if settings.post_delay > 0:
            delay = np.zeros(round(self.ni.smpl_rate * settings.post_delay))
            frame = np.hstack([frame, delay])

        if settings.pre_delay > 0:
            delay = np.zeros(round(self.ni.smpl_rate * settings.pre_delay))
            #TODO whty is predelay after camera trigger?
            # Maybe because the camera 'stores' the trigger?
            frame = np.hstack([frame, delay])

        return frame


class AOTF:
    def __init__(self, ni:NIDAQ):
        self.ni = ni
        self.blank_voltage = 10
        core = self.ni.event_thread.bridge.get_core()
        self.power_488 = float(core.get_property('488_AOTF',r"Power (% of max)"))
        self.power_561 = float(core.get_property('561_AOTF',r"Power (% of max)"))

    def one_frame(self, settings:MMSettings, channel:dict):
        blank = make_pulse(self.ni, 0, self.blank_voltage, 0)
        if channel['name'] == '488':
            aotf_488 = make_pulse(self.ni, 0, self.power_488/10, 0)
            aotf_561 = make_pulse(self.ni, 0, 0, 0)
        elif channel['name'] == '561':
            aotf_488 = make_pulse(self.ni, 0, 0, 0)
            aotf_561 = make_pulse(self.ni, 0, self.power_561/10, 0)
        elif channel['name'] == 'LED':
            aotf_488 = make_pulse(self.ni, 0, 0, 0)
            aotf_561 = make_pulse(self.ni, 0, 0, 0)
        aotf = np.vstack((blank, aotf_488, aotf_561))
        aotf = self.add_delays(aotf, settings)
        return aotf

    def add_delays(self, frame:np.ndarray, settings: MMSettings):
        if settings.post_delay > 0:
            delay = np.zeros((frame.shape[0], round(self.ni.smpl_rate * settings.post_delay)))
            frame = np.hstack([frame, delay])

        if settings.pre_delay > 0:
            delay = np.zeros((frame.shape[0], round(self.ni.smpl_rate * settings.pre_delay)))
            frame = np.hstack([delay, frame])

        return frame


class Brightfield:
    def __init__(self, ni:NIDAQ):
        self.flippers = Flippers()
        self.led_on = False
        self.flippers_up = False
        self.ni = ni
        self.led(False)
        self.flippers.brightfield(False)

    def toggle_led(self):
        self.led(not self.led_on)
        self.led_on = not self.led_on

    def toggle_flippers(self, up:bool = None):
        up = not self.flippers_up if up is None else up
        self.flippers.brightfield(up)
        self.flippers_up = up

    def led(self, on:bool = True, power: float = 1.):
        self.led_on = on
        power = power if on else 0
        with nidaqmx.Task() as task:
            task.ao_channels.add_ao_voltage_chan("Dev1/ao6")
            task.write(power, auto_start=True)

    def one_frame(self, settings):
        led = make_pulse(self.ni, 0, 0.3, 0)
        return led

if __name__ == '__main__':
    import sys
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication(sys.argv)

    event_thread = EventThread()
    event_thread.start()

    ni = NIDAQ(event_thread)

    settings_view = SettingsView(event_thread)
    settings_view.show()

    sys.exit(app.exec_())
