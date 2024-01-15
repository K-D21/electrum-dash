# -*- coding: utf-8 -*-

import asyncio
import time
from enum import IntEnum
from blspy import BasicSchemeMPL, G1Element, G2Element

from .bitcoin import address_to_script
from .dash_msg import (DSPoolStatusUpdate, DSMessageIDs, ds_msg_str,
                       ds_pool_state_str, DashDsaMsg, DashDsiMsg, DashDssMsg)
from .dash_tx import str_ip, CTxIn, CTxOut
from .util import bfh, bh2u

PRIVATESEND_QUEUE_TIMEOUT = 30
PRIVATESEND_SESSION_MSG_TIMEOUT = 40


class PSDenoms(IntEnum):
    '''Denoms values designated in P2P protocol'''
    D10 = 1
    D1 = 2
    D0_1 = 4
    D0_01 = 8
    D0_001 = 16


class MixSessionTimeout(Exception):
    """Thrown when waiting for next message from MN is timed out"""


class MixSessionPeerClosed(Exception):
    """Thrown when waiting for next message from MN, and MN closes session"""


class PSMixSession:
    '''P2P session with mixing masternode'''

    def __init__(self, psman, denom_value, denom, dsq, wfl_lid):
        self.logger = psman.logger
        self.denom_value = denom_value
        self.denom = denom
        self.wfl_lid = wfl_lid

        network = psman.wallet.network
        self.dash_net = network.dash_net
        self.mn_list = network.mn_list

        self.dash_peer = None
        self.sml_entry = None

        if dsq:
            protxHash = bh2u(dsq.protxHash)
            self.sml_entry = self.mn_list.get_mn_by_protx_hash(protxHash)
        if not self.sml_entry:
            try_cnt = 0
            while True:
                try_cnt += 1
                self.sml_entry = self.mn_list.get_random_mn()
                if self.peer_str not in psman.recent_mixes_mns:
                    break
                if try_cnt >= 10:
                    raise Exception('Can not select random'
                                    ' not recently used  MN')
        if not self.sml_entry:
            raise Exception('No SML entries found')
        psman.recent_mixes_mns.append(self.peer_str)
        self.msg_queue = asyncio.Queue()

        self.session_id = 0
        self.state = None
        self.msg_id = None
        self.entries_count = 0
        self.protxHash = None
        self.fReady = False
        self.nTime = 0
        self.start_time = time.time()

    @property
    def peer_str(self):
        return f'{str_ip(self.sml_entry.ipAddress)}:{self.sml_entry.port}'

    async def run_peer(self):
        if self.dash_peer:
            raise Exception('Session already have running DashPeer')
        self.dash_peer = await self.dash_net.run_mixing_peer(self.peer_str,
                                                             self.sml_entry,
                                                             self)
        if not self.dash_peer:
            raise Exception(f'Peer {self.peer_str} connection failed')
        self.logger.info(f'Started mixing session for {self.wfl_lid},'
                         f' peer: {self.peer_str}, denom={self.denom_value}'
                         f' (nDenom={self.denom})')

    def close_peer(self):
        if not self.dash_peer:
            return
        self.dash_peer.close()
        self.logger.info(f'Stopped mixing session for {self.wfl_lid},'
                         f' peer: {self.peer_str}')

    def verify_ds_msg_sig(self, ds_msg):
        '''Verify BLS signature of dsq message from masternode based on
        SML entry for masternode from protx_list'''
        if not self.sml_entry:
            return False
        mn_pub_key = self.sml_entry.pubKeyOperator
        is_legacy = self.sml_entry.version == 1

        pubkey = G1Element.from_bytes(mn_pub_key, is_legacy)
        signature = G2Element.from_bytes(ds_msg.vchSig)

        valid = BasicSchemeMPL.verify(pubkey, ds_msg.msg_hash(), signature)
        return valid

    def verify_final_tx(self, tx, denominate_wfl):
        '''Verify final tx from dsf message'''
        inputs = denominate_wfl.inputs
        outputs = denominate_wfl.outputs
        icnt = 0
        ocnt = 0
        for i in tx.inputs():
            if i.prevout.to_str() in inputs:
                icnt += 1
        for o in tx.outputs():
            if o.address in outputs:
                ocnt += 1
        if icnt == len(inputs) and ocnt == len(outputs):
            return True
        else:
            return False

    async def send_dsa(self, pay_collateral_tx):
        '''Send dsa message to join or create mixing queue'''
        msg = DashDsaMsg(self.denom, pay_collateral_tx)
        await self.dash_peer.send_msg('dsa', msg.serialize())
        self.logger.debug(f'{self.wfl_lid}: dsa sent')

    async def send_dsi(self, inputs, pay_collateral_tx, outputs):
        '''Send dsi message containing inputs to mix, output addresses'''
        scriptSig = b''
        sequence = 0xffffffff
        vecTxDSIn = []
        for i in inputs:
            prev_h, prev_n = i.split(':')
            prev_h = bfh(prev_h)[::-1]
            prev_n = int(prev_n)
            vecTxDSIn.append(CTxIn(prev_h, prev_n, scriptSig, sequence))
        vecTxDSOut = []
        for o in outputs:
            scriptPubKey = bfh(address_to_script(o))
            vecTxDSOut.append(CTxOut(self.denom_value, scriptPubKey))
        msg = DashDsiMsg(vecTxDSIn, pay_collateral_tx, vecTxDSOut)
        await self.dash_peer.send_msg('dsi', msg.serialize())
        self.logger.debug(f'{self.wfl_lid}: dsi sent')

    async def send_dss(self, signed_inputs):
        '''Send dss message containing signed inputs of dsf message final tx'''
        msg = DashDssMsg(signed_inputs)
        await self.dash_peer.send_msg('dss', msg.serialize())

    async def read_next_msg(self, denominate_wfl, timeout=None):
        '''Read next msg from msg_queue, process and return (cmd, res) tuple'''
        try:
            if timeout is None:
                timeout = PRIVATESEND_SESSION_MSG_TIMEOUT
            res = await asyncio.wait_for(self.msg_queue.get(), timeout)
        except asyncio.TimeoutError:
            raise MixSessionTimeout('Session Timeout, Reset')
        if not res:  # dash_peer is closed
            raise MixSessionPeerClosed('peer connection closed')
        elif type(res) == Exception:
            raise res
        cmd = res.cmd
        payload = res.payload
        if cmd == 'dssu':
            res = self.on_dssu(payload)
            return cmd, res
        elif cmd == 'dsq':
            self.logger.debug(f'{self.wfl_lid}: dsq read: {payload}')
            res = self.on_dsq(payload)
            return cmd, res
        elif cmd == 'dsf':
            self.logger.debug(f'{self.wfl_lid}: dsf read: {payload}')
            res = self.on_dsf(payload, denominate_wfl)
            return cmd, res
        elif cmd == 'dsc':
            self.logger.wfl_ok(f'{self.wfl_lid}: dsc read: {payload}')
            res = self.on_dsc(payload)
            return cmd, res
        else:
            self.logger.debug(f'{self.wfl_lid}: unknown msg read, cmd: {cmd}')
            return None, None

    def on_dssu(self, dssu):
        '''Process dssu message from masternode, containing state update'''
        session_id = dssu.sessionID
        if not self.session_id:
            if session_id:
                self.session_id = session_id

        if self.session_id != session_id:
            raise Exception(f'Wrong session id {session_id},'
                            f' was {self.session_id}')

        self.state = dssu.state
        self.msg_id = dssu.messageID

        state = ds_pool_state_str(self.state)
        msg = ds_msg_str(self.msg_id)
        if (dssu.statusUpdate == DSPoolStatusUpdate.ACCEPTED
                and dssu.messageID != DSMessageIDs.ERR_QUEUE_FULL):
            self.logger.debug(f'{self.wfl_lid}: dssu read:'
                              f' state={state}, msg={msg}')
        elif dssu.statusUpdate == DSPoolStatusUpdate.ACCEPTED:
            raise Exception('MN queue is full')
        elif dssu.statusUpdate == DSPoolStatusUpdate.REJECTED:
            raise Exception(f'Get reject status update from MN: {msg}')
        else:
            raise Exception(f'Unknown dssu statusUpdate: {dssu.statusUpdate}')

    def on_dsq(self, dsq):
        '''Process dsq messages broadcasted from masternodes,
        and inform about existing queue states'''
        denom = dsq.nDenom
        if denom != self.denom:
            raise Exception(f'Wrong denom in dsq msg: {denom},'
                            f' session denom is {self.denom}.')
        # signature verified in dash_peer on receiving dsq message for session
        # signature not verifed for dsq with fReady not set (go to recent dsq)
        if not dsq.fReady:  # additional check
            raise Exception('Get dsq with fReady not set')
        if self.fReady:
            raise Exception('Another dsq on session with fReady set')
        self.protxHash = dsq.protxHash
        self.fReady = dsq.fReady
        self.nTime = dsq.nTime

    def on_dsf(self, dsf, denominate_wfl):
        '''Process dsf message from masternode, containing final tx to sign'''
        session_id = dsf.sessionID
        if self.session_id != session_id:
            raise Exception(f'Wrong session id {session_id},'
                            f' was {self.session_id}')
        if not self.verify_final_tx(dsf.txFinal, denominate_wfl):
            raise Exception('Wrong txFinal')
        return dsf.txFinal

    def on_dsc(self, dsc):
        '''Process dsc message from masternode,
        which indicates mixing session is complete'''
        session_id = dsc.sessionID
        if self.session_id != session_id:
            raise Exception(f'Wrong session id {session_id},'
                            f' was {self.session_id}')
        msg_id = dsc.messageID
        if msg_id != DSMessageIDs.MSG_SUCCESS:
            raise Exception(ds_msg_str(msg_id))
