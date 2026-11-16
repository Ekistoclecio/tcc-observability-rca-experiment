#!/usr/bin/python
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import sys
from pythonjsonlogger import jsonlogger

# TODO(yoshifumi) this class is duplicated since other Python services are
# not sharing the modules for logging.
class CustomJsonFormatter(jsonlogger.JsonFormatter):
  def add_fields(self, log_record, record, message_dict):
    super(CustomJsonFormatter, self).add_fields(log_record, record, message_dict)
    if not log_record.get('timestamp'):
      log_record['timestamp'] = record.created
    if log_record.get('severity'):
      log_record['severity'] = log_record['severity'].upper()
    else:
      log_record['severity'] = record.levelname

def getJSONLogger(name):
  logger = logging.getLogger(name)
  handler = logging.StreamHandler(sys.stdout)
  formatter = CustomJsonFormatter('%(timestamp)s %(severity)s %(name)s %(message)s')
  handler.setFormatter(formatter)
  logger.addHandler(handler)
  logger.setLevel(logging.INFO)
  logger.propagate = False
  return logger

def configure_otel_logging(logger, service_name, endpoint):
  from opentelemetry.sdk.resources import Resource
  from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
  from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
  from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

  provider = LoggerProvider(resource=Resource.create({"service.name": service_name}))
  provider.add_log_record_processor(BatchLogRecordProcessor(
    OTLPLogExporter(endpoint=endpoint, insecure=True)))
  logger.addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=provider))
  return provider
