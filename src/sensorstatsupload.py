#!/usr/bin/env python3
"""

Todo:
    * Service Discovery of Bridge
    * Find Sensor First, then just use that endpoint
    * Use Different Polling per Type (Presence>Temperature)
"""
import argparse
import json
import logging
import time
import os

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from envyaml import EnvYAML
from influxdb import InfluxDBClient

import constants


def load_config(config_file):
    """
    Loads from a specified yaml config file, the yaml loader used will
    replace environment variables.

    Args:
        config_file (String): Path to configuration file
    """
    cfg = EnvYAML(config_file)
    logging.debug("Configuration: %s", cfg.export())
    return cfg


def log_sensor_data(sensors):
    """
    Will log in a formatted way all the interesting sensor measurements

    Args:
        sensors (Dictionary): JSON Sensor Data in the influx measurement format
    """
    summary = "Sensor Data..."
    for sensor in sensors:
        summary += f"{sensor['measurement']}:{sensor['fields']['value']}({sensor['time'][-8:]} UTC) "
    logging.info( summary )


def get_hue_sensor_data(sensor_id, bridge_ip, user_key, retry_count=5):
    """
    Queries a Hue Bridge for its sensor payload returning JSON.
    Will retry a number of times if required.

    Args:
        sensor_id (Integer): As per the Bridge, -1 for all Senors
        bridge_ip (String): The IP Address of the Bridge to query
        user_key (String): The User Key for the Queries to the Bridge
        retry_count (Integer): Optional argument, defaulted to 5
    """
    endpoint = f"http://{bridge_ip}/api/{user_key}/sensors"
    if sensor_id != -1:
        endpoint += f"/{sensor_id}"

    requests_session = requests.Session()
    retries = Retry(total=retry_count, backoff_factor=2, status_forcelist=[502, 503, 504])
    requests_session.mount(endpoint, HTTPAdapter(max_retries=retries))

    logging.info("Querying %s", endpoint)
    result = requests_session.get(endpoint)
    json_result = result.json()
    logging.info("Responded Status Code: %s %i Elements in %.0fms", result.status_code, len(json_result), result.elapsed.total_seconds()*1000)
    logging.debug(json.dumps(json_result, indent=4, sort_keys=True))

    return json_result


def parse_sensor_json(sensors_json):
    """
    Pulls out only attributes we are interested in from the JSON
    sensors we support (eg. Temperature, Presence and LightLevel)

    Will also convert to standard unit for types (ie. degrees c or lux)

    Args:
        sensors_json (?): The full JSON data from the sensor(s)

    Returns:
        List: JSON Sensor Data in InfluxDB Measurement Value Format
    """
    parsed_sensors = []
    for sensor_json in sensors_json.values() :
        try:
            if sensor_json['type'] in constants.SUPPORTED_SENSORS:
                sensor = {}
                sensor['tags'] = { 'id':sensor_json['uniqueid'],
                                    'name':sensor_json['name'] }
                                    #TODO: Think if need utf8 decoding for above
                sensor['time'] = sensor_json['state']['lastupdated'] # supplied in UTC
                sensor['fields'] = {}
                if sensor_json['type'] == constants.HUE_ZIGBEE_TEMPERATURE :
                    sensor['measurement'] = constants.TEMPERATURE_MEASURE
                    sensor['fields']['value'] = sensor_json['state']['temperature'] / 100.00 # in degrees C
                if sensor_json['type'] == constants.HUE_ZIGBEE_PRESENCE :
                    sensor['measurement'] = constants.PRESENCE_MEASURE
                    sensor['fields']['value'] = sensor_json['state']['presence']
                if sensor_json['type'] == constants.HUE_ZIGBEE_LIGHT :
                    sensor['measurement'] = constants.LIGHT_MEASURE
                    sensor['fields']['value'] = pow(10, (sensor_json['state']['lightlevel'] - 1) / 10000) # in lumens/lux
                parsed_sensors.append( sensor )
        except TypeError:
            logging.warning("Encountered unexpected sensor format, skipping sensor")
            logging.warning("Unexpected sensor json: %s", sensor_json)

    return parsed_sensors

_FIRST_DB_CONNECTION = [True] #Mutable List to avoid Global
def persist_measurement(data, influx_db_cfg, retry_counter=5):
    """
    Persist supplied data into configured Influx DB.  Will retry a number of
    times if there are ConnectionErrors.

    Args:
        data (Dictionary): JSON Data in InfluxDB  Measurement Value Format
        influx_db_cfg (Dictionary): Config with DB Connection Details
        retry_counter (Integer): Optional argument, defaulted to 5
    """
    if _FIRST_DB_CONNECTION[0] :
        logging.info("Connecting to InfluxDB:%s on %s:%s as %s", influx_db_cfg['database'],
                        influx_db_cfg['host'], influx_db_cfg['port'], influx_db_cfg['username'] )
        _FIRST_DB_CONNECTION[0] = False

    try:
        client = InfluxDBClient(host=influx_db_cfg['host'], port=influx_db_cfg['port'],
                                username=influx_db_cfg['username'], password=influx_db_cfg['password'])
        client.switch_database(influx_db_cfg['database'])

        client.write_points(data)
        logging.info("Successfully persisted %i values in InfluxDB", len(data))
    except requests.exceptions.ConnectionError as error:
        if retry_counter:
            logging.warning("Unable to Connect to InfluxDB, will sleep 5s and retry %i times", retry_counter)
            time.sleep(5)
            persist_measurement(data, influx_db_cfg, retry_counter-1)
        else:
            logging.warning("Unable to Connect to InfluxDB, exhausted retries")
            raise ConnectionRefusedError("Unable to Connect to InfluxDB") from error


def main(iterations, sleep, config_file):
    """
    Runs the main processing loop querying hue sensors and storing in influx

    Args:
        iterations (Integer): Number of times to interate, ie. loop. Default is infinite.
        sleep (Interger): How long to sleep for between iterations. Default uses config.
    """
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%d/%m/%Y %I:%M:%S %p %Z',
                        level=os.environ.get("LOGLEVEL", logging.INFO))

    cfg = load_config(config_file)
    hue_bridge = cfg['hue_bridge.url']
    user_key = cfg['hue_bridge.user_key']

    if sleep == -1 :
        sleep = min( cfg['intervals'].values() )

    i = 0
    try:
        while iterations == -1 or i < iterations :
            i+=1
            if i==1 and iterations == -1 :
                logging.info("Begining to infinitely loop with %is sleep", sleep)
            elif i==1 or not i%10 :
                logging.info("Iteration %i of %i for main processing loop with %is sleep", i, iterations, sleep)

            sensor_id = -1 # Querying all sensors for now, see file TODO's
            json_sensor_data = get_hue_sensor_data(sensor_id, hue_bridge, user_key)
            if sensor_id != -1 :
                json_sensor_data = {sensor_id:json_sensor_data}

            parsed_sensors = parse_sensor_json(json_sensor_data)
            log_sensor_data(parsed_sensors)
            persist_measurement(parsed_sensors, cfg['influx_db'])

            time.sleep(sleep)
    except KeyboardInterrupt:
        logging.warning("Process interrupted, ending loop")


if __name__ == '__main__' :
    parser = argparse.ArgumentParser(description='Queries and prints home metrics output from Hue Bridge')
    parser.add_argument('-i','--iterations', type=int, default=-1, help='Number of times to interate, ie. loop. Default is infinite.' )
    parser.add_argument('-s','--sleep', type=int, default=-1, help='How long to sleep for between iterations. Default uses config.' )
    parser.add_argument('-c','--config', type=str, default='cfg/homestatsconfig.yaml', help='Configuration file' )
    args = parser.parse_args()
    main(args.iterations, args.sleep, args.config)
