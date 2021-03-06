#!/usr/bin/env python
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from rci import root

import asyncio
import argparse


def run():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-v", "--verbose", help="verbose", action="store_true")
    group.add_argument("-q", "--quiet", help="quiet mode", action="store_true")
    parser.add_argument("filename", type=str, help="config file")
    args = parser.parse_args()
    loop = asyncio.get_event_loop()
    r = root.Root(loop, args.filename, args.verbose)
    loop.run_until_complete(r.run())
