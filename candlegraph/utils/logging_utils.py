#!/usr/bin/env python3

import logging

service_logger = logging.getLogger('subgraph')
console_handler = logging.StreamHandler()
formatter = logging.Formatter('%(levelname)s: %(asctime)s %(message)s')
console_handler.setFormatter(formatter)

service_logger.setLevel(logging.INFO)
service_logger.addHandler(console_handler)
