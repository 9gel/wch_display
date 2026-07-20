#!/usr/bin/env python3
"""Example csm-panel provider.

A provider is any program that prints, on stdout, a JSON object mapping panel
field ids (2..21) to integer values. The service runs it once per interval and
streams the values to the panel via 0x66. Where the numbers come from is entirely
up to you — local sensors, a monitoring API, a database, anything.

This stub just emits smoothly-varying demo values so you can see the panel react
without any real data source. Point your config's `[provider] command` at it:

    [provider]
    command = "python examples/provider_stub.py"
"""
import json
import math
import time

# valid field ids are 2..21 (field 1 is not addressable by the 0x66 frame)
FIELDS = range(2, 22)


def main():
    t = time.time() / 6.0
    values = {f: int(45 + 20 * math.sin(t + f)) for f in FIELDS}
    print(json.dumps({str(k): v for k, v in values.items()}))


if __name__ == "__main__":
    main()
