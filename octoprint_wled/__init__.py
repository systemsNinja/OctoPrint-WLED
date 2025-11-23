import logging
from typing import Any, Dict, List, Optional, Tuple, Union

import octoprint.plugin

from octoprint_wled import _version, api, constants, events, progress, runner
from octoprint_wled.util import calculate_heating_progress, get_wled_params
from octoprint_wled.wled import WLED

__version__ = _version.get_versions()["version"]


class WLEDPlugin(
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.ProgressPlugin,
):
    def __init__(self):
        super().__init__()
        self.wled: Optional[WLED] = None
        self.api: Optional[api.PluginAPI] = None
        self.events: Optional[events.PluginEventHandler] = None
        self.runner: Optional[runner.WLEDRunner] = None
        self.progress: Optional[progress.PluginProgressHandler] = None
        self.lights_on: bool = True

        self._logger: logging.Logger

        # Heating & cooling detection flags
        self.heating: bool = False
        self.cooling: bool = False
        self.target_temperature: Dict[str, int] = {"tool": 0, "bed": 0}
        self.current_heater_heating: Optional[str] = None
        self.tool_to_target: int = 0

        self.current_progress: int = 0

    def initialize(self) -> None:
        self.init_wled()
        self.api = api.PluginAPI(self)
        self.events = events.PluginEventHandler(self)
        self.runner = runner.WLEDRunner(self)
        self.progress = progress.PluginProgressHandler(self)

    def init_wled(self) -> None:
        # TEST MODE: Simplified Init. 
        # We are ignoring the segment setting here and hardcoding it in the button below.
        if self._settings.get(["connection", "host"]):
            self.wled = WLED(**get_wled_params(self._settings))
        else:
            self.wled = None

    def activate_lights(self) -> None:
        self._logger.info("TESTING: Turning Segment 1 ON")
        
        # Define the function that does the work
        def turn_seg1_on():
            # 1. Get the segment object for ID 1
            seg = self.wled.segment(1)
            # 2. Update it (Bright 255, On True)
            return seg.update(on=True, bri=255)

        # Send it to the runner
        self.runner.wled_call(turn_seg1_on)

        self.send_message("lights", {"on": True})
        self.lights_on = True

    def deactivate_lights(self) -> None:
        self._logger.info("TESTING: Turning Segment 1 OFF")
        
        def turn_seg1_off():
            # 1. Get the segment object for ID 1
            seg = self.wled.segment(1)
            # 2. Update it (Bright 0, On False)
            return seg.update(on=False, bri=0)

        # Send it to the runner
        self.runner.wled_call(turn_seg1_off)

        self.send_message("lights", {"on": False})
        self.lights_on = False

    # -------------------------------------------------------------------------
    # EVERYTHING BELOW THIS LINE IS UNCHANGED
    # -------------------------------------------------------------------------
    
    # Gcode tracking hook
    def process_gcode_queue(
        self,
        comm,
        phase,
        cmd: str,
        cmd_type,
        gcode: str,
        subcode=None,
        tags=None,
        *args,
        **kwargs,
    ):
        if gcode in constants.BLOCKING_TEMP_GCODES.keys():
            self.heating = True
            self.cooling = False  
            self.current_heater_heating = constants.BLOCKING_TEMP_GCODES[gcode]

        else:
            if self.heating:
                self.heating = False
                if self._printer.is_printing():
                    self.progress.return_to_print_progress()

    def temperatures_received(
        self,
        comm,
        parsed_temps: Dict[str, Tuple[float, Union[float, None]]],
        *args,
        **kwargs,
    ):
        tool_key = self._settings.get(["progress", "heating", "tool_key"])

        try:
            tool_target = parsed_temps[f"T{tool_key}"][1]
        except KeyError:
            tool_target = self.target_temperature["tool"]

        if tool_target is None or tool_target <= 0:
            tool_target = self.target_temperature["tool"]

        try:
            bed_target = parsed_temps["B"][1]
        except KeyError:
            bed_target = self.target_temperature["tool"]

        if bed_target is None or bed_target <= 0:
            bed_target = self.target_temperature["bed"]

        self.target_temperature["tool"] = tool_target
        self.target_temperature["bed"] = bed_target

        if self.heating:
            if self.current_heater_heating == "tool":
                heater = f"T{tool_key}"
            else:
                heater = "B"
            try:
                current = parsed_temps[heater][0]
            except KeyError:
                self._logger.warning(
                    f"{heater} not found, can't show progress - check config"
                )
                self.heating = False
                return parsed_temps

            value = calculate_heating_progress(
                current, self.target_temperature[self.current_heater_heating]
            )
            self._logger.debug(f"Heating, progress {value}%")
            if self._settings.get(["progress", "heating", self.current_heater_heating]):
                self.progress.on_heating_progress(value)

        elif self.cooling:
            bed_or_tool = self._settings.get(["progress", "cooling", "bed_or_tool"])
            if bed_or_tool == "tool":
                heater = "T{}".format(
                    self._settings.get(["progress", "heating", "tool_key"])
                )
            else:
                heater = "B"

            current = parsed_temps[heater][0]

            if current < self._settings.get_int(["progress", "cooling", "threshold"]):
                self.cooling = False
                self.events.update_effect("success")
                return parsed_temps

            value = calculate_heating_progress(
                current, self.target_temperature[bed_or_tool]
            )

            self._logger.debug(f"Cooling, progress {value}%")
            self.progress.on_cooling_progress(value)

        return parsed_temps

    def process_at_command(
        self, comm, phase, cmd: str, parameters: str, tags=None, *args, **kwargs
    ):
        if cmd != constants.AT_WLED or not self._settings.get_boolean(
            ["features", "atcommand"]
        ):
            return

        cmd = cmd.upper()
        parameters = parameters.upper()

        if parameters == constants.AT_PARAM_ON:
            self.activate_lights()
        elif parameters == constants.AT_PARAM_OFF:
            self.deactivate_lights()

    def get_api_commands(self) -> Dict[str, List[Optional[str]]]:
        return self.api.get_api_commands()

    def on_api_command(self, command, data):
        return self.api.on_api_command(command, data)

    def on_api_get(self, request):
        return self.api.on_api_get(request)

    def send_message(self, msg_type: str, msg_content: dict):
        self._plugin_manager.send_plugin_message(
            "wled", {"type": msg_type, "content": msg_content}
        )

    def on_event(self, event, payload):
        self.events.on_event(event, payload)

    def on_print_progress(self, storage, path, progress_value):
        self.progress.on_print_progress(progress_value)

    def on_shutdown(self):
        self.runner.kill()

    def get_settings_defaults(self) -> Dict[str, Any]:
        return {
            "connection": {
                "host": "",
                "segment_id": "",
                "auth": False,
                "username": None,
                "password": "",
                "port": 80,
                "request_timeout": 2,
                "tls": False,
                "verify_ssl": True,
            },
            "effects": {
                "idle": {"enabled": True, "settings": []},
                "disconnected": {"enabled": True, "settings": []},
                "failed": {"enabled": True, "settings": []},
                "started": {"enabled": False, "settings": []},
                "success": {"enabled": True, "settings": []},
                "paused": {"enabled": True, "settings": []},
            },
            "progress": {
                "print": {"enabled": True, "settings": []},
                "heating": {"enabled": True, "settings": [], "tool": True, "bed": True, "tool_key": "0"},
                "cooling": {"enabled": True, "settings": [], "bed_or_tool": "tool", "tool_key": "0", "threshold": "40"}
            },
            "development": False,
            "features": {"atcommand": True, "return_to_idle": 0},
        }

    def get_settings_version(self):
        return 1

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self.init_wled()
        self.events.restart()

    def get_assets(self) -> Dict[str, List[str]]:
        if self._settings.get_boolean(["development"]):
            js = ["src/wled.js"]
        else:
            js = ["dist/wled.js"]
        return {"js": js, "css": ["dist/wled.css"]}

    def get_template_vars(self):
        return {"version": self._plugin_version}

    def get_update_information(self) -> dict:
        return {
            "wled": {
                "displayName": "WLED Connection",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "cp2004",
                "repo": "OctoPrint-WLED",
                "current": self._plugin_version,
                "stable_branch": {"name": "Stable", "branch": "main", "comittish": ["main"]},
                "prerelease_branches": [{"name": "Release Candidate", "branch": "pre-release", "comittish": ["pre-release", "main"]}],
                "pip": "https://github.com/cp2004/OctoPrint-WLED/releases/download/{target_version}/release.zip",
            }
        }

__plugin_name__ = "WLED Connection"
__plugin_version__ = __version__
__plugin_pythoncompat__ = ">=3.7,<4"

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = WLEDPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.process_gcode_queue,
        "octoprint.comm.protocol.temperatures.received": __plugin_implementation__.temperatures_received,
        "octoprint.comm.protocol.atcommand.queuing": __plugin_implementation__.process_at_command,
    }