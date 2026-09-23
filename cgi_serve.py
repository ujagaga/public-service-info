#!/usr/bin/env python3

import os
from wsgiref.handlers import CGIHandler
from index import application

os.environ.setdefault("REQUEST_METHOD", "GET")

CGIHandler().run(application)