# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for
# full license information.

import sys
import time
import io
import csv


# Imports for inferencing
import onnxruntime as rt
from inference import run_onnx
import numpy as np
import cv2

# Imports for communication w/IOT Hub
from iothub_client import IoTHubModuleClient, IoTHubClientError, IoTHubTransportProvider
from iothub_client import IoTHubMessage, IoTHubMessageDispositionResult, IoTHubError

# Imports for the http server
from flask import Flask, request
import json

# Imports for storage
import os
from azure.storage.blob import BlockBlobService, PublicAccess, AppendBlobService
import random
import string
import csv
from datetime import datetime
from pytz import timezone  
import time 

# Get Labels 
labels_file = open("labels.txt")
labels_string = labels_file.read()
labels = labels_string.split(",")
labels_file.close()
label_lookup = {}
for i, val in enumerate(labels):
	label_lookup[val] = i

# Loading ONNX model
print("loading Tiny YOLO...")
start_time = time.time()
sess = rt.InferenceSession('TinyYOLO.onnx')
print("loaded after", time.time()-start_time,"s")

# Path to CSV FILE (edit if you want)
LATEST_FULL_PATH = '/home/storagedata/objectcountlatest.csv'
DAILY_FULL_DIR = '/home/storagedata/'
LATEST_CSV_NAME = 'objectcountlatest.csv'
DAILY_CSV_NAME = ''

# Set TimeZone (Change from pacific if you live in another timezone)
TIME_ZONE = timezone('US/Pacific')

# This is a boolean marking whether to use cloud storage or not.
# Change to true and follow tutorial instructions if you would like to use it.
CLOUD_STORAGE = True
print("CLOUD STORAGE STATUS:", CLOUD_STORAGE)

CONTAINER_NAME = 'localcontainer'
ts = datetime.now(TIME_ZONE)
timestring = ts.strftime("%Y-%m-%d %H:%M:%S")
current_date = timestring.split()[0]
DAILY_STRING = "timestamp,location," + labels_string + "\n"
if (CLOUD_STORAGE):
	block_blob_service = BlockBlobService(connection_string='DefaultEndpointsProtocol=http;BlobEndpoint=http://azureblobstorageoniotedge:11002/<local storage container name>;AccountName=<local storage container name>;AccountKey=<local storage container key>')
	ts = datetime.now(TIME_ZONE)
	DAILY_CSV_NAME = 'objectcount' + current_date + '.csv'
	block_blob_service.create_container(CONTAINER_NAME)
	block_blob_service.create_blob_from_text(container_name = CONTAINER_NAME, blob_name= DAILY_CSV_NAME, text = DAILY_STRING, encoding = 'utf-8')
	
# messageTimeout - the maximum time in milliseconds until a message times out.
# The timeout period starts at IoTHubModuleClient.send_event_async.
# By default, messages do not expire.
MESSAGE_TIMEOUT = 10000

# Choose HTTP, AMQP or MQTT as transport protocol.  Currently only MQTT is supported.
PROTOCOL = IoTHubTransportProvider.MQTT


class HubManager(object):
	def __init__(
			self,
			protocol=IoTHubTransportProvider.MQTT):
		self.client_protocol = protocol
		self.client = IoTHubModuleClient()
		self.client.create_from_environment(protocol)

		# set the time until a message times out
		self.client.set_option("messageTimeout", MESSAGE_TIMEOUT)

	# Forwards the message received onto the next stage in the process.
	def forward_event_to_output(self, outputQueueName, event, send_context):
		self.client.send_event_async(
			outputQueueName, event, send_confirmation_callback, send_context)


def send_confirmation_callback(message, result, user_context):
	"""
	Callback received when the message that we're forwarding is processed.
	"""
	print("Confirmation[%d] received for message with result = %s" % (user_context, result))

print("trying to make IOT Hub manager")
hub_manager = None

# Will stop trying to make IOT Hub Manager after 2 minutes
start_time = time.time()
timeout = time.time() + 60*2
while time.time() < timeout:
	time.sleep(1)
	try:
		hub_manager = HubManager(PROTOCOL)
		break
	except IoTHubError as iothub_error:
		print("Unexpected error %s from IoTHub" % iothub_error)

print("INITIALIZED AFTER",time.time()-start_time,"s")
if not hub_manager:
	print("Took too long to make hub_manager, exiting program.")
	print("Try restarting IotEdge or this module.")
	sys.exit(1)

app = Flask(__name__)

# Define dictionary for adaptive control of appending to the csv
has_changed = {}

@app.route('/', methods=['POST'])
def frame_handler():
	""" 
	Handles incoming post requests. Gets frame from request and calls inferencing function on frame.
	Sends result to IOT Hub.
	"""
	try:
		global current_date
		global DAILY_STRING
		global has_changed
		data = json.loads(request.data)
		cameras = data['cameras'].split(',')
		locations = data['locations'].split(',')
		outputstring = ""
		start_time = time.time()
		CURR_STRING = ""
		LATEST_STRING = ""
		# ITERATE THROUGH ALL CAMERAS
		for i, camera in enumerate(cameras):
			imageData = data[camera+":frame"]
			frame = get_tinyyolo_frame_from_encode(imageData)
			location = locations[i]
			timestamp = data[camera+":timestamp"].split()[1]
			output, outputstr = run_onnx(frame, location, timestamp, sess)
			outputstring += outputstr
			#LOOK AT OBJECTS AND CHECK PREVIOUS STATUS TO APPEND
			objects = output[2:]
			num_objects = len(objects) 
			print("NUMBER OBJECTS DETECTED:", num_objects)
			column_outputs = [0 for i in range(20)]
			for i in objects:
				idx = label_lookup[i[0]]
				column_outputs[idx] += 1
			objects = ",".join(str(i) for i in column_outputs)
			prev_objects = has_changed.get(camera)
			LATEST_STRING += timestamp + ',' + location + ',' + objects + '\n'
			if (objects != prev_objects):
				CURR_STRING += timestamp + ',' + location + ',' + objects + '\n'
				has_changed[camera] = objects
		DAILY_STRING += CURR_STRING
		if (CLOUD_STORAGE):
			ts = datetime.now(TIME_ZONE)
			timestring = ts.strftime("%Y-%m-%d %H:%M:%S")
			new_day = timestring.split()[0]
			if (new_day != current_date):
				DAILY_CSV_NAME = 'objectcount' + new_day + '.csv'
				DAILY_STRING = "timestamp,location," + labels_string + "\n"
				current_date = new_day
			DAILY_CSV_NAME = 'objectcount' + current_date + '.csv'
			block_blob_service.create_blob_from_text(container_name = CONTAINER_NAME, blob_name= DAILY_CSV_NAME, text = DAILY_STRING, encoding = 'utf-8')
			block_blob_service.create_blob_from_text(container_name = CONTAINER_NAME, blob_name= LATEST_CSV_NAME, text = "timestamp,location," + labels_string + "\n" + LATEST_STRING, encoding = 'utf-8')
			print("SUCCESSFULLY STORED", file=sys.stderr)				
				
		print("PROCESSED",len(cameras),"IN",time.time()-start_time,"s")
		output_IOT = IoTHubMessage(outputstring)
		hub_manager.forward_event_to_output("inferenceoutput", output_IOT, 0)
		print(outputstring, file=sys.stderr)
		return outputstring
	except Exception as e:
	  print('EXCEPTION:', str(e))
	  return 'Error processing image', 500

def get_tinyyolo_frame_from_encode(msg):
	"""
	Formats jpeg encoded msg to frame that can be processed by tiny_yolov2
	"""
	inp = np.array(msg).reshape((len(msg),1))
	frame = cv2.imdecode(inp.astype(np.uint8), 1)
	frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
	frame = np.array(frame).astype(np.float32)
	frame = cv2.resize(frame, (416, 416))
	frame = frame.transpose(2, 0, 1)
	frame = np.reshape(frame, (1, 3, 416, 416))
	return frame

if __name__ == '__main__':
	app.run(host='0.0.0.0', port=5000)
