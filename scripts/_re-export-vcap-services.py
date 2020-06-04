#!/usr/bin/env python3

import json
import os

try:
    vcap_services_txt = os.environ.get("VCAP_SERVICES", None)
    vcap_services = json.loads(vcap_services_txt)
    print(f'export REDIS_URL="{vcap_services["redis"][0]["credentials"]["uri"]}"')
except Exception:
    pass

