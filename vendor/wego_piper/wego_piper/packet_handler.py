#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2025 WeGo-Robotics Inc. EDU team. All rights reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
from piper_sdk import *


class PacketHandler(object):
    def getTxRxResult(self, result):
        raise NotImplementedError()

    def getRxPacketError(self, error):
        raise NotImplementedError()

    def txPacket(self, port, txpacket):
        raise NotImplementedError()

    def rxPacket(self, port):
        raise NotImplementedError()

    def txRxPacket(self, port, txpacket):
        raise NotImplementedError()

    def ping(self, port, id):
        raise NotImplementedError()

    def action(self, port, id):
        raise NotImplementedError()

    def readTx(self, port, id, address, length):
        raise NotImplementedError()

    def readRx(self, port, id, length):
        raise NotImplementedError()

    def readTxRx(self, port, id, address, length):
        raise NotImplementedError()

    def read1ByteTx(self, port, id, address):
        raise NotImplementedError()

    def read1ByteRx(self, port, id):
        raise NotImplementedError()

    def read1ByteTxRx(self, port, id, address):
        raise NotImplementedError()

    def read2ByteTx(self, port, id, address):
        raise NotImplementedError()

    def read2ByteRx(self, port, id):
        raise NotImplementedError()

    def read2ByteTxRx(self, port, id, address):
        raise NotImplementedError()

    def read4ByteTx(self, port, id, address):
        raise NotImplementedError()

    def read4ByteRx(self, port, id):
        raise NotImplementedError()

    def read4ByteTxRx(self, port, id, address):
        raise NotImplementedError()

    def writeTxOnly(self, port, id, address, length, data):
        raise NotImplementedError()

    def writeTxRx(self, port, id, address, length, data):
        raise NotImplementedError()

    def write1ByteTxOnly(self, port, id, address, data):
        raise NotImplementedError()

    def write1ByteTxRx(self, port, id, address, data):
        raise NotImplementedError()

    def write2ByteTxOnly(self, port, id, address, data):
        raise NotImplementedError()

    def write2ByteTxRx(self, port, id, address, data):
        raise NotImplementedError()

    def write4ByteTxOnly(self, port, id, address, data):
        raise NotImplementedError()

    def write4ByteTxRx(self, port, id, address, data):
        raise NotImplementedError()

    def regWriteTxOnly(self, port, id, address, length, data):
        raise NotImplementedError()

    def regWriteTxRx(self, port, id, address, length, data):
        raise NotImplementedError()

    def syncReadTx(self, port, start_address, data_length, param, param_length):
        raise NotImplementedError()

    def syncWriteTxOnly(
        self, port, start_address, data_length, param, param_length
    ):
        raise NotImplementedError()

