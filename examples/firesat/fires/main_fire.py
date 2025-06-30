# -*- coding: utf-8 -*-
"""
*This application demonstrates a simulation of a schedule of fires given geospatial locations and specified datetimes (at one minute resolution)*

The application contains a single :obj:`Environment` class which listens to the time status published by the manager application and publishes fire information at the specified ignition :obj:`datetime`. The application also contains callback messages that updates :obj:`datetime` in the fires :obj:`DataFrame` for each of ignition (including latitude-longitude :obj:`GeographicPosition`), detection, and reporting.

"""

import logging
import os
from datetime import datetime, timezone

import pandas as pd

pd.options.mode.chained_assignment = None


from fire_config_files.schemas import FireDetected, FireReported, FireStarted, FireState

from nost_tools.application_utils import ShutDownObserver
from nost_tools.configuration import ConnectionConfig
from nost_tools.managed_application import ManagedApplication
from nost_tools.observer import Observer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import warnings

warnings.simplefilter(action="ignore", category=FutureWarning)


# define an observer to manage fire updates and record to a dataframe fires
class Environment(Observer):
    """
    *The Environment object class inherits properties from the Observer object class in the NOS-T tools library*

    Attributes:
        app (:obj:`ManagedApplication`): An application containing a test-run namespace, a name and description for the app, client credentials, and simulation timing instructions
        fires (:obj:`DataFrame`): Dataframe of scenario scheduled fires including fireId (*int*), fire ignition (:obj:`datetime`), and fire latitude-longitude location (:obj:`GeographicPosition`)
    """

    def __init__(self, app, fires):
        self.app = app
        self.fires = fires

    def on_change(self, source, property_name, old_value, new_value):
        """
        *Standard on_change callback function format inherited from Observer object class*

        In this instance, the callback function checks the simulation :obj:`datetime` against each scheduled fire ignition :obj:`datetime` for the scenario. If past the scheduled start of a fire, a :obj:`FireStarted` message is sent to *PREFIX.fire.location*:

        .. literalinclude:: /../../examples/firesat/fires/main_fire.py
            :pyobject: Environment.on_change
            :lines: 11-
        """
        if property_name == "time":
            new_fires = self.fires[
                (self.fires.start <= new_value) & (self.fires.start > old_value)
            ]
            for index, fire in new_fires.iterrows():
                self.app.send_message(
                    self.app.app_name,
                    "location",
                    FireStarted(
                        fireId=fire.fireId,
                        start=fire.start,
                        latitude=fire.latitude,
                        longitude=fire.longitude,
                    ).model_dump_json(),
                )

    def on_fire(self, ch, method, properties, body):
        body = body.decode("utf-8")

        start = FireStarted.model_validate_json(body)
        for key, fire in self.fires.iterrows():
            if key == start.fireId:
                self.fires["fireState"][key] = FireState.started
                break

    def on_detected(self, ch, method, properties, body):
        body = body.decode("utf-8")

        detect = FireDetected.model_validate_json(body)
        for key, fire in self.fires.iterrows():
            if key == detect.fireId:
                self.fires["fireState"][key] = FireState.detected
                self.fires["detected"][key] = detect.detected
                self.fires["detected_by"][key] = detect.detected_by
                break

    def on_reported(self, ch, method, properties, body):
        body = body.decode("utf-8")

        report = FireReported.model_validate_json(body)
        for key, fire in self.fires.iterrows():
            if key == report.fireId:
                self.fires["fireState"][key] = FireState.reported
                self.fires["reported"][key] = report.reported
                self.fires["reported_by"][key] = report.reported_by
                self.fires["reported_to"][key] = report.reported_to
                break


def on_fire(ch, method, properties, body):
    """
    *Callback function parses a FireStarted message and switches FireState from "undefined" to "started"*

    .. literalinclude:: /../../examples/firesat/fires/main_fire.py
        :pyobject: Environment.on_fire
    """
    for index, observer in enumerate(app.simulator._observers):
        if isinstance(observer, Environment):
            app.simulator._observers[index].on_fire(ch, method, properties, body)


def on_detected(ch, method, properties, body):
    """
    *Callback function parses a FireDetected message, switches FireState from "started" to "detected", and records time of first detection and name of satellite detecting the fire*

    .. literalinclude:: /../../examples/firesat/fires/main_fire.py
        :pyobject: Environment.on_detected
    """
    for index, observer in enumerate(app.simulator._observers):
        if isinstance(observer, Environment):
            app.simulator._observers[index].on_detected(ch, method, properties, body)


def on_reported(ch, method, properties, body):
    """
    *Callback function parses a FireReported message, switches FireState from "detected" to "reported", and records time of first report, name of satellite reporting the fire, and groundId receiving the report*

    .. literalinclude:: /../../examples/firesat/fires/main_fire.py
        :pyobject: Environment.on_reported
    """
    for index, observer in enumerate(app.simulator._observers):
        if isinstance(observer, Environment):
            app.simulator._observers[index].on_reported(ch, method, properties, body)


if __name__ == "__main__":
    # Define application name
    NAME = "fire"

    # Load config
    config = ConnectionConfig(
        yaml_file="examples/firesat/firesat2025.yaml",
        app_name=NAME,
    )

    # Create the managed application
    app = ManagedApplication(app_name=NAME)

    # Import CSV file from fire_scenarios subdirectory with scenario defining locations and ignition datetimes of fires
    csvFile = os.path.join("examples/firesat/fires", "fire_scenarios", "riverAOI.csv")

    # Read the csv file and convert to a DataFrame with initial column defining the index
    df = pd.read_csv(csvFile, index_col=0)
    fires = pd.DataFrame(
        data={
            "fireId": df.index,
            "start": pd.to_datetime(df["start_time"], utc=True),
            "latitude": df["latitude"],
            "longitude": df["longitude"],
        }
    )

    # Add blank columns to data frame for logging state, detection time, reporting time, and detector satellite
    fires.insert(1, "fireState", FireState.undefined)
    fires.insert(3, "detected", datetime(1900, 1, 1, tzinfo=timezone.utc))
    fires.insert(4, "detected_by", "Undetected")
    fires.insert(5, "reported", datetime(1900, 1, 1, tzinfo=timezone.utc))
    fires.insert(6, "reported_by", "Unreported")
    fires.insert(7, "reported_to", None)

    # Add the environment observer to monitor for fire status events
    app.simulator.add_observer(Environment(app, fires))

    # Add a shutdown observer to shut down after a single test case
    app.simulator.add_observer(ShutDownObserver(app))

    # Start up the application
    app.start_up(
        config.rc.simulation_configuration.execution_parameters.general.prefix,
        config,
    )

    # Add message callbacks
    app.add_message_callback("fire", "location", on_fire)
    app.add_message_callback("constellation", "detected", on_detected)
    app.add_message_callback("constellation", "reported", on_reported)

    while True:
        pass
