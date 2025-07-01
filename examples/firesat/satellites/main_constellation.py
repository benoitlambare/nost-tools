# -*- coding: utf-8 -*-
"""
*This application demonstrates a constellation of satellites for monitoring fires propagated from Two-Line Elements (TLEs).*

The application contains one :obj:`Constellation` (:obj:`Entity`) object class, one :obj:`PositionPublisher` (:obj:`WallclockTimeIntervalPublisher`) class, and two :obj:`Observer` object classes to monitor for :obj:`FireDetected` and :obj:`FireReported` events, respectively. The application also adds several methods outside of these classes containing standardized calculations sourced from Ch. 5 of *Space Mission Analysis and Design* by Wertz and Larson. The example also includes a startup script that demonstrates how to add each of these objects to the simulator before executing the test case, as well as how to map callback functions to the newly started client.

"""

import copy
import logging
from datetime import timedelta

import numpy as np
import pandas as pd
from constellation_config_files.schemas import (
    FireDetected,
    FireReported,
    FireStarted,
    GroundLocation,
    SatelliteStatus,
)
from skyfield.api import EarthSatellite, load, wgs84

from nost_tools.application_utils import ShutDownObserver
from nost_tools.configuration import ConnectionConfig
from nost_tools.entity import Entity
from nost_tools.managed_application import ManagedApplication
from nost_tools.observer import Observer
from nost_tools.publisher import WallclockTimeIntervalPublisher

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def compute_min_elevation(altitude, field_of_regard):
    """
    Computes the minimum elevation angle required for a satellite to observe a point from current location.

    Args:
        altitude (float): Altitude (meters) above surface of the observation
        field_of_regard (float): Angular width (degrees) of observation

    Returns:
        float : min_elevation
            The minimum elevation angle (degrees) for observation
    """
    earth_equatorial_radius = 6378137.000000000
    earth_polar_radius = 6356752.314245179
    earth_mean_radius = (2 * earth_equatorial_radius + earth_polar_radius) / 3

    # eta is the angular radius of the region viewable by the satellite
    sin_eta = np.sin(np.radians(field_of_regard / 2))
    # rho is the angular radius of the earth viewed by the satellite
    sin_rho = earth_mean_radius / (earth_mean_radius + altitude)
    # epsilon is the min satellite elevation for obs (grazing angle)
    cos_epsilon = sin_eta / sin_rho
    if cos_epsilon > 1:
        return 0.0
    return np.degrees(np.arccos(cos_epsilon))


def compute_sensor_radius(altitude, min_elevation):
    """
    Computes the sensor radius for a satellite at current altitude given minimum elevation constraints.

    Args:
        altitude (float): Altitude (meters) above surface of the observation
        min_elevation (float): Minimum angle (degrees) with horizon for visibility

    Returns:
        float : sensor_radius
            The radius (meters) of the nadir pointing sensors circular view of observation
    """
    earth_equatorial_radius = 6378137.0
    earth_polar_radius = 6356752.314245179
    earth_mean_radius = (2 * earth_equatorial_radius + earth_polar_radius) / 3
    # rho is the angular radius of the earth viewed by the satellite
    sin_rho = earth_mean_radius / (earth_mean_radius + altitude)
    # eta is the nadir angle between the sub-satellite direction and the target location on the surface
    eta = np.degrees(np.arcsin(np.cos(np.radians(min_elevation)) * sin_rho))
    # calculate swath width half angle from trigonometry
    sw_HalfAngle = 90 - eta - min_elevation
    if sw_HalfAngle < 0.0:
        return 0.0
    return earth_mean_radius * np.radians(sw_HalfAngle)


def get_elevation_angle(t, sat, loc):
    """
    Returns the elevation angle (degrees) of satellite with respect to the topocentric horizon.

    Args:
        t (:obj:`Time`): Time object of skyfield.timelib module
        sat (:obj:`EarthSatellite`): Skyview EarthSatellite object from skyfield.sgp4lib module
        loc (:obj:`GeographicPosition`): Geographic location on surface specified by latitude-longitude from skyfield.toposlib module

    Returns:
        float : alt.degrees
            Elevation angle (degrees) of satellite with respect to the topocentric horizon
    """
    difference = sat - loc
    topocentric = difference.at(t)
    # NOTE: Topos uses term altitude for what we are referring to as elevation
    alt, az, distance = topocentric.altaz()
    return alt.degrees


def check_in_view(t, satellite, topos, min_elevation):
    """
    Checks if the elevation angle of the satellite with respect to the ground location is greater than the minimum elevation angle constraint.

    Args:
        t (:obj:`Time`): Time object of skyfield.timelib module
        satellite (:obj:`EarthSatellite`): Skyview EarthSatellite object from skyfield.sgp4lib module
        topos (:obj:`GeographicPosition`): Geographic location on surface specified by latitude-longitude from skyfield.toposlib module
        min_elevation (float): Minimum elevation angle (degrees) for ground to be in view of satellite, as calculated by compute_min_elevation

    Returns:
        bool : isInView
            True/False indicating visibility of ground location to satellite
    """
    isInView = False
    elevationFromFire = get_elevation_angle(t, satellite, topos)
    if elevationFromFire >= min_elevation:
        isInView = True
    return isInView


def check_in_range(t, satellite, grounds):
    """
    Checks if the satellite is in range of any of the operational ground stations.

    Args:
        t (:obj:`Time`): Time object of skyfield.timelib module
        satellite (:obj:`EarthSatellite`): Skyview EarthSatellite object from skyfield.sgp4lib module
        grounds (:obj:`DataFrame`): Dataframe of ground station locations, minimum elevation angles for communication, and operational status (T/F)

    Returns:
        bool, int :
            isInRange
                True/False indicating visibility of satellite to any operational ground station
            groundId
                groundId of the ground station currently in comm range (NOTE: If in range of two ground stations simultaneously, will return first groundId)
    """
    isInRange = False
    groundId = None
    for k, ground in grounds.iterrows():
        if ground.operational:
            groundLatLon = wgs84.latlon(ground.latitude, ground.longitude)
            satelliteElevation = get_elevation_angle(t, satellite, groundLatLon)
            if satelliteElevation >= ground.elevAngle:
                isInRange = True
                groundId = k
                break
    return isInRange, groundId


# define an entity to manage satellite updates
class Constellation(Entity):
    """
    *This object class inherits properties from the Entity object class in the NOS-T tools library*

    Args:
        cName (str): A string containing the name for the constellation application
        app (:obj:`ManagedApplication`): An application containing a test-run namespace, a name and description for the app, client credentials, and simulation timing instructions
        id (list): List of unique *int* ids for each satellite in the constellation
        names (list): List of unique *str* for each satellite in the constellation (must be same length as **id**)\n
        ES (list): Optional list of :obj:`EarthSatellite` objects to be included in the constellation (NOTE: at least one of **ES** or **tles** MUST be specified, or an exception will be thrown)\n
        tles (list): Optional list of Two-Line Element *str* to be converted into :obj:`EarthSatellite` objects and included in the simulation

    Attributes:
        fires (list): List of fires with unique fireId (*int*), ignition (:obj:`datetime`), and latitude-longitude location (:obj:`GeographicPosition`) - *NOTE:* initialized as [ ]
        grounds (:obj:`DataFrame`): Dataframe containing information about ground stations with unique groundId (*int*), latitude-longitude location (:obj:`GeographicPosition`), min_elevation (*float*) angle constraints, and operational status (*bool*) - *NOTE:* initialized as **None**
        satellites (list): List of :obj:`EarthSatellite` objects included in the constellation - *NOTE:* must be same length as **id**
        detect (list): List of detected fires with unique fireId (*int*), detected :obj:`datetime`, and name (*str*) of detecting satellite - *NOTE:* initialized as [ ]
        report (list): List of reported fires with unique fireId (*int*), reported :obj:`datetime`, name (*str*) of reporting satellite, and groundId (*int*) of ground station reported to - *NOTE:* initialized as [ ]
        positions (list): List of current latitude-longitude-altitude locations (:obj:`GeographicPosition`) of each satellite in the constellation - *NOTE:* must be same length as **id**
        next_positions (list): List of next latitude-longitude-altitude locations (:obj:`GeographicPosition`) of each satellite in the constellation - *NOTE:* must be same length as **id**
        min_elevations_fire (list): List of *floats* indicating current elevation angle (degrees) constraint for visibility by each satellite - *NOTE:* must be same length as **id**, updates every time step

    """

    ts = load.timescale()
    PROPERTY_FIRE_REPORTED = "reported"
    PROPERTY_FIRE_DETECTED = "detected"
    PROPERTY_POSITION = "position"

    def __init__(self, cName, app, id, names, ES=None, tles=None):
        super().__init__(cName)
        self.app = app
        self.id = id
        self.names = names
        self.fires = []
        self.grounds = None
        self.satellites = []
        if ES is not None:
            for satellite in ES:
                self.satellites.append(satellite)
        if tles is not None:
            for i, tle in enumerate(tles):
                self.satellites.append(
                    EarthSatellite(tle[0], tle[1], self.names[i], self.ts)
                )
        self.detect = []
        self.report = []
        self.positions = self.next_positions = [None for satellite in self.satellites]
        self.min_elevations_fire = [
            compute_min_elevation(
                wgs84.subpoint(satellite.at(satellite.epoch)).elevation.m,
                config.rc.application_configuration["FIELD_OF_REGARD"][i],
            )
            for i, satellite in enumerate(self.satellites)
        ]

    def initialize(self, init_time):
        """
        Activates the :obj:`Constellation` at a specified initial scenario time

        Args:
            init_time (:obj:`datetime`): Initial scenario time for simulating propagation of satellites
        """
        super().initialize(init_time)
        self.grounds = pd.DataFrame(
            {
                "groundId": pd.Series([], dtype="int"),
                "latitude": pd.Series([], dtype="float"),
                "longitude": pd.Series([], dtype="float"),
                "elevAngle": pd.Series([], dtype="float"),
                "operational": pd.Series([], dtype="bool"),
            }
        )
        self.positions = self.next_positions = [
            wgs84.subpoint(satellite.at(self.ts.from_datetime(init_time)))
            for satellite in self.satellites
        ]

    def tick(self, time_step):
        """
        Computes the next :obj:`Constellation` state after the specified scenario duration and the next simulation scenario time

        Args:
            time_step (:obj:`timedelta`): Duration between current and next simulation scenario time
        """
        super().tick(time_step)
        self.next_positions = [
            wgs84.subpoint(
                satellite.at(self.ts.from_datetime(self.get_time() + time_step))
            )
            for satellite in self.satellites
        ]
        for i, satellite in enumerate(self.satellites):
            then = self.ts.from_datetime(self.get_time() + time_step)
            self.min_elevations_fire[i] = compute_min_elevation(
                float(self.next_positions[i].elevation.m),
                config.rc.application_configuration["FIELD_OF_REGARD"][i],
            )
            for j, fire in enumerate(self.fires):
                # if self.detect[j][self.names[i]] is None:
                topos = wgs84.latlon(fire["latitude"], fire["longitude"])
                isInView = check_in_view(
                    then, satellite, topos, self.min_elevations_fire[i]
                )
                if isInView:
                    self.detect[j][self.names[i]] = (
                        self.get_time() + time_step
                    )  # TODO could use event times
                    if self.detect[j]["firstDetector"] is None:
                        self.detect[j]["firstDetect"] = True
                        self.detect[j]["firstDetector"] = self.names[i]
                if (self.detect[j][self.names[i]] is not None) and (
                    self.report[j][self.names[i]] is None
                ):
                    isInRange, groundId = check_in_range(then, satellite, self.grounds)
                    if isInRange:
                        self.report[j][self.names[i]] = self.get_time() + time_step
                        if self.report[j]["firstReporter"] is None:
                            self.report[j]["firstReport"] = True
                            self.report[j]["firstReporter"] = self.names[i]
                            self.report[j]["firstReportedTo"] = groundId

    def tock(self):
        """
        Commits the next :obj:`Constellation` state and advances simulation scenario time

        """
        self.positions = self.next_positions
        for i, newly_detected_fire in enumerate(self.detect):
            if newly_detected_fire["firstDetect"]:
                detector = newly_detected_fire["firstDetector"]
                self.notify_observers(
                    self.PROPERTY_FIRE_DETECTED,
                    None,
                    {
                        "fireId": newly_detected_fire["fireId"],
                        "detected": newly_detected_fire[detector],
                        "detected_by": detector,
                    },
                )
                # self.detect[i]["firstDetect"] = False
        for i, newly_reported_fire in enumerate(self.report):
            if newly_reported_fire["firstReport"]:
                reporter = newly_reported_fire["firstReporter"]
                self.notify_observers(
                    self.PROPERTY_FIRE_REPORTED,
                    None,
                    {
                        "fireId": newly_reported_fire["fireId"],
                        "reported": newly_reported_fire[reporter],
                        "reported_by": reporter,
                        "reported_to": newly_reported_fire["firstReportedTo"],
                    },
                )
            self.report[i]["firstReport"] = False
        super().tock()

    def on_fire(self, ch, method, properties, body):  # , client, userdata, message):
        """
        Callback function appends a dictionary of information for a new fire to fires :obj:`list` when message detected on the *PREFIX.fires.location* topic

        Args:
            ch (:obj:`Channel`): Channel object used to communicate with the event broker
            method (:obj:`Method`): Method object containing information about the message delivery, such as delivery tag and exchange
            properties (:obj:`Properties`): Properties object containing metadata about the message, such as content type and message ID
            body (:obj:`bytes`): Contains the message content as a byte string, which is decoded to a UTF-8 string and then parsed into a :obj:`FireStarted` object
        """
        body = body.decode("utf-8")

        started = FireStarted.model_validate_json(body)  # message.payload)
        self.fires.append(
            {
                "fireId": started.fireId,
                "start": started.start,
                "latitude": started.latitude,
                "longitude": started.longitude,
            },
        )
        satelliteDictionary = dict.fromkeys(
            self.names
        )  # Creates dictionary where keys are satellite names and values are defaulted to NoneType
        satelliteDictionary["fireId"] = (
            started.fireId
        )  # Adds fireId to dictionary, which will coordinate with position of dictionary in list of dictionaries
        detectDictionary = copy.deepcopy(satelliteDictionary)
        detectDictionary["firstDetect"] = False
        detectDictionary["firstDetector"] = None
        self.detect.append(detectDictionary)
        reportDictionary = copy.deepcopy(satelliteDictionary)
        reportDictionary["firstReport"] = False
        reportDictionary["firstReporter"] = None
        reportDictionary["firstReportedTo"] = None
        self.report.append(reportDictionary)
        # ch.connection.close()
        # ch.basic_ack(delivery_tag=method.delivery_tag)

    def on_ground(self, ch, method, properties, body):  # client, userdata, message):
        """
        Callback function appends a dictionary of information for a new ground station to grounds :obj:`list` when message detected on the *PREFIX/ground/location* topic. Ground station information is published at beginning of simulation, and the :obj:`list` is converted to a :obj:`DataFrame` when the Constellation is initialized.

        Args:
            ch (:obj:`Channel`): Channel object used to communicate with the event broker
            method (:obj:`Method`): Method object containing information about the message delivery, such as delivery tag and exchange
            properties (:obj:`Properties`): Properties object containing metadata about the message, such as content type and message ID
            body (:obj:`bytes`): Contains the message content as a byte string, which is decoded to a UTF-8 string and then parsed into a :obj:`FireStarted` object
        """
        body = body.decode("utf-8")

        location = GroundLocation.model_validate_json(body)  # message.payload)

        if location.groundId in self.grounds.groundId:
            self.grounds[
                self.grounds.groundId == location.groundId
            ].latitude = location.latitude
            self.grounds[
                self.grounds.groundId == location.groundId
            ].longitude = location.longitude
            self.grounds[
                self.grounds.groundId == location.groundId
            ].elevAngle = location.elevAngle
            self.grounds[
                self.grounds.groundId == location.groundId
            ].operational = location.operational
            print(f"Station {location.groundId} updated at time {self.get_time()}.")
        else:
            location = {
                "groundId": location.groundId,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "elevAngle": location.elevAngle,
                "operational": location.operational,
            }

            # Create a DataFrame from the location dictionary
            new_data = pd.DataFrame([location])

            # Concatenate the new data with the existing DataFrame
            self.grounds = pd.concat([self.grounds, new_data], ignore_index=True)


# define a publisher to report satellite status
class PositionPublisher(WallclockTimeIntervalPublisher):
    """
    *This object class inherits properties from the WallclockTimeIntervalPublisher object class from the publisher template in the NOS-T tools library*

    The user can optionally specify the wallclock :obj:`timedelta` between message publications and the scenario :obj:`datetime` when the first of these messages should be published.

    Args:
        app (:obj:`ManagedApplication`): An application containing a test-run namespace, a name and description for the app, client credentials, and simulation timing instructions
        constellation (:obj:`Constellation`): Constellation :obj:`Entity` object class
        time_status_step (:obj:`timedelta`): Optional duration between time status 'heartbeat' messages
        time_status_init (:obj:`datetime`): Optional scenario :obj:`datetime` for publishing the first time status 'heartbeat' message

    """

    def __init__(
        self, app, constellation, time_status_step=None, time_status_init=None
    ):
        super().__init__(app, time_status_step, time_status_init)
        self.constellation = constellation
        self.isInRange = [
            False for i, satellite in enumerate(self.constellation.satellites)
        ]

    def publish_message(self):
        """
        *Abstract publish_message method inherited from the WallclockTimeIntervalPublisher object class from the publisher template in the NOS-T tools library*

        This method sends a :obj:`SatelliteStatus` message to the *PREFIX/constellation/location* topic for each satellite in the constellation (:obj:`Constellation`).

        """
        for i, satellite in enumerate(self.constellation.satellites):
            next_time = constellation.ts.from_datetime(
                constellation.get_time() + 60 * self.time_status_step
            )
            satSpaceTime = satellite.at(next_time)
            subpoint = wgs84.subpoint(satSpaceTime)
            sensorRadius = compute_sensor_radius(
                subpoint.elevation.m, constellation.min_elevations_fire[i]
            )
            self.isInRange[i], groundId = check_in_range(
                next_time, satellite, constellation.grounds
            )
            # print(
            #     f"[DEBUG] lat={subpoint.latitude.degrees}, lon={subpoint.longitude.degrees}"
            # )

            self.app.send_message(
                self.app.app_name,
                "location",
                SatelliteStatus(
                    id=i,
                    noradId=satellite.model.satnum,
                    name=satellite.name,
                    latitude=subpoint.latitude.degrees,
                    longitude=subpoint.longitude.degrees,
                    altitude=subpoint.elevation.m,
                    radius=sensorRadius,
                    commRange=self.isInRange[i],
                    time=constellation.get_time(),
                ).model_dump_json(),
            )


# define an observer to send fire detection events
class FireDetectedObserver(Observer):
    """
    *This object class inherits properties from the Observer object class from the observer template in the NOS-T tools library*

    Args:
        app (:obj:`ManagedApplication`): An application containing a test-run namespace, a name and description for the app, client credentials, and simulation timing instructions

    .. literalinclude:: /../../examples/firesat/satellites/main_constellation.py
        :pyobject: FireDetectedObserver.on_change
        :lines: 8-
    """

    def __init__(self, app):
        self.app = app

    def on_change(self, source, property_name, old_value, new_value):
        """
        *Standard on_change callback function format inherited from Observer object class in NOS-T tools library*

        In this instance, the callback function checks for notification of the "detected" property and publishes :obj:`FireDetected` message to *PREFIX/constellation/detected* topic:

        """
        if property_name == Constellation.PROPERTY_FIRE_DETECTED:
            self.app.send_message(
                self.app.app_name,
                "detected",
                FireDetected(
                    fireId=new_value["fireId"],
                    detected=new_value["detected"],
                    detected_by=new_value["detected_by"],
                ).model_dump_json(),
            )


# define an observer to send fire reporting events
class FireReportedObserver(Observer):
    """
    *This object class inherits properties from the Observer object class from the observer template in the NOS-T tools library*

    Args:
        app (:obj:`ManagedApplication`): An application containing a test-run namespace, a name and description for the app, client credentials, and simulation timing instructions

    .. literalinclude:: /../../examples/firesat/satellites/main_constellation.py
        :pyobject: FireReportedObserver.on_change
        :lines: 8-
    """

    def __init__(self, app):
        self.app = app

    def on_change(self, source, property_name, old_value, new_value):
        """
        *Standard on_change callback function format inherited from Observer object class in NOS-T tools library*

        In this instance, the callback function checks for notification of the "reported" property and publishes :obj:`FireReported` message to *PREFIX/constellation/reported* topic:

        """
        if property_name == Constellation.PROPERTY_FIRE_REPORTED:
            self.app.send_message(
                self.app.app_name,
                "reported",
                FireReported(
                    fireId=new_value["fireId"],
                    reported=new_value["reported"],
                    reported_by=new_value["reported_by"],
                    reported_to=new_value["reported_to"],
                ).model_dump_json(),
            )


if __name__ == "__main__":
    # Define application name
    NAME = "constellation"

    # Load config
    config = ConnectionConfig(
        yaml_file="examples/firesat/firesat2025.yaml", app_name=NAME
    )

    # Create the managed application
    app = ManagedApplication(app_name=NAME)

    # Load current TLEs for active satellites from Celestrak
    activesats_url = (
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle"
    )
    activesats = load.tle_file(
        activesats_url, filename="examples/firesat/satellites/active.txt", reload=True
    )

    norads = [
        39634,
        40697,
        41335,
        42063,
        42969,
        43437,
        62671,
        40013,
        59126,
        47510,
        48917,
        # 59103,
        # 59100,
        # 60539,
        # 62705,
    ]

    ES = []
    indices = []
    by_norad = {
        sat.model.satnum: sat for sat in activesats if sat.model.satnum in norads
    }

    for name_i, norad in enumerate(norads):
        ES.append(by_norad[norad])
        indices.append(name_i)

    # Initialize the Constellation object class (in this example from EarthSatellite type)
    constellation = Constellation("constellation", app, indices, norads, ES)

    # Add observer classes to the Constellation object class
    constellation.add_observer(FireDetectedObserver(app))
    constellation.add_observer(FireReportedObserver(app))

    # Add the Constellation entity to the application's simulator
    app.simulator.add_entity(constellation)

    # Add a shutdown observer to shut down after a single test case
    app.simulator.add_observer(ShutDownObserver(app))

    # Add a position publisher to update satellite state every 5 seconds of wallclock time
    app.simulator.add_observer(
        PositionPublisher(app, constellation, timedelta(seconds=1))
    )

    # Start up the application
    app.start_up(
        config.rc.simulation_configuration.execution_parameters.general.prefix,
        config,
    )

    # Add message callbacks
    app.add_message_callback("fire", "location", constellation.on_fire)
    app.add_message_callback("ground", "location", constellation.on_ground)

    while True:
        pass
