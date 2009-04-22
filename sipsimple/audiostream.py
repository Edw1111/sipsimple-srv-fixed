from __future__ import with_statement
from thread import allocate_lock

from zope.interface import implements

from application.notification import NotificationCenter, NotificationData

from sipsimple.interfaces import IMediaStream
from sipsimple.util import TimestampedNotificationData, NotificationHandler
from sipsimple.lookup import DNSLookup
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import RTPTransport, AudioTransport, SIPCoreError
from sipsimple.engine import Engine
from sipsimple.green import GreenBase


# TODO:
# - Add audio recording
# - Add hold notifications
# - Resolve remaining TODOs

class AudioStream(NotificationHandler):
    implements(IMediaStream)

    def __init__(self, account):
        self.state = "NULL"
        self.account = account
        self.notification_center = NotificationCenter()
        self.on_hold_by_local = False
        self.on_hold_by_remote = False
        self._audio_transport = None
        self._rtp_transport = None
        self._lock = allocate_lock()

    @property
    def on_hold(self):
        return self.on_hold_by_local or self.on_hold_by_remote

    def validate_incoming(self, remote_sdp, stream_index):
        with self._lock:
            # TODO: actually validate the SDP
            self._incoming_remote_sdp = remote_sdp
            self._incoming_stream_index = stream_index
            return True

    def initialize(self, session):
        with self._lock:
            if self.state != "NULL":
                raise RuntimeError("AudioStream.initialize() may only be called in the NULL state")
            self.state = "INITIALIZING"
            self._sip_transport = session._inv.transport
            if hasattr(self.account, "ice") and self.account.ice.enabled and self.account.ice.use_stun:
                if self.account.stun_servers:
                    # Assume these are IP addresses
                    stun_servers = list(self.account.stun_servers)
                    self._init_rtp_transport(stun_servers)
                else:
                    dns_lookup = DNSLookup()
                    self.notification_center.add_observer(self, sender=dns_lookup)
                    dns_lookup.lookup_service(self.account.credentials.uri, "stun")
            else:
                self._init_rtp_transport()

    def _NH_DNSLookupDidFail(self, dns_lookup, data):
        with self._lock:
            self.notification_center.remove_observer(self, sender=dns_lookup)
            if self.state == "ENDED":
                return
            self._init_rtp_transport()

    def _NH_DNSLookupDidSucceed(self, dns_lookup, data):
        with self._lock:
            self.notification_center.remove_observer(self, sender=dns_lookup)
            if self.state == "ENDED":
                return
            self._init_rtp_transport(data.result)

    def _init_rtp_transport(self, stun_servers=None):
        settings = SIPSimpleSettings()
        self._rtp_args = dict(local_rtp_address=settings.local_ip.normalized)
        self._rtp_args["use_srtp"] = ((self._sip_transport == "tls" or self.account.audio.use_srtp_without_tls)
                                      and self.account.audio.srtp_encryption != "disabled")
        self._rtp_args["srtp_forced"] = self._rtp_args["use_srtp"] and self.account.audio.srtp_encryption == "mandatory"
        self._rtp_args["use_ice"] = hasattr(self.account, "ice") and self.account.ice.enabled
        del self._sip_transport
        self._stun_servers = [(None, None)]
        if stun_servers:
            self._stun_servers.extend(reversed(stun_servers))
        self._try_next_rtp_transport()

    def _try_next_rtp_transport(self, failure_reason=None):
        # TODO: log failure_reason if it is not None?
        if self._stun_servers:
            stun_ip, stun_port = self._stun_servers.pop()
            observer_added = False
            try:
                rtp_transport = RTPTransport(ice_stun_address=stun_ip, ice_stun_port=stun_port, **self._rtp_args)
                self.notification_center.add_observer(self, sender=rtp_transport)
                observer_added = True
                rtp_transport.set_INIT()
            except SIPCoreError, e:
                if observer_added:
                    self.notification_center.remove_observer(self, sender=rtp_transport)
                self._try_next_rtp_transport(e.args[0])
        else:
            self.state = "ENDED"
            self.notification_center.post_notification("MediaStreamDidFail", self,
                                                       TimestampedNotificationData(reason=failure_reason))

    def _NH_RTPTransportDidFail(self, rtp_transport, data):
        with self._lock():
            self.notification_center.remove_observer(self, sender=rtp_transport)
            if self.state == "ENDED":
                return
            self._try_next_rtp_transport(data.reason)

    def _NH_RTPTransportDidInitialize(self, rtp_transport, data):
        with self._lock:
            self.notification_center.remove_observer(self, sender=rtp_transport)
            if self.state == "ENDED":
                return
            del self._rtp_args
            del self._stun_servers
            try:
                if hasattr(self, "_incoming_remote_sdp"):
                    try:
                        audio_transport = AudioTransport(rtp_transport, self._incoming_remote_sdp,
                                                         self._incoming_stream_index,
                                                         codecs=(list(self.account.audio.codec_list)
                                                                 if self.account.audio.codec_list else None))
                    finally:
                        del self._incoming_remote_sdp
                        del self._incoming_stream_index
                else:
                    audio_transport = AudioTransport(rtp_transport, codecs=(list(self.account.audio.codec_list)
                                                                            if self.account.audio.codec_list else None))
            except SIPCoreError, e:
                self.state = "ENDED"
                self.notification_center.post_notification("MediaStreamDidFail", self,
                                                           TimestampedNotificationData(reason=e.args[0]))
            self._rtp_transport = rtp_transport
            self._audio_transport = audio_transport
            self.notification_center.add_observer(self, sender=audio_transport)
            self.state = "INITIALIZED"
            self.notification_center.post_notification("MediaStreamDidInitialize", self, TimestampedNotificationData())

    def get_local_media(self, for_offer, on_hold=False):
        with self._lock:
            if self.state not in ["INITIALIZED", "ESTABLISHED"]:
                raise RuntimeError("AudioStream.get_local_media() may only be " +
                                   "called in the INITIALIZED or ESTABLISHED states")
            if on_hold and self.state == "ESTABLISHED" and not self.on_hold_by_local:
                Engine().disconnect_audio_transport(self._audio_transport)
            if for_offer:
                old_direction = self._audio_transport.direction
                if old_direction is None:
                    new_direction = "sendrecv"
                elif "send" in old_direction:
                    new_direction = ("sendonly" if on_hold else "sendrecv")
                else:
                    new_direction = ("inactive" if on_hold else "recvonly")
            else:
                new_direction = None
            return self._audio_transport.get_local_media(for_offer, new_direction)

    def start(self, local_sdp, remote_sdp, stream_index):
        with self._lock:
            if self.state != "INITIALIZED":
                raise RuntimeError("AudioStream.get_local_media() may only be " +
                                   "called in the INITIALIZED or ESTABLISHED states")
            self._audio_transport.start(local_sdp, remote_sdp, stream_index)
            self.on_hold_by_local = "recv" not in self._audio_transport.direction
            self.on_hold_by_remote = "send" not in self._audio_transport.direction
            if not self.on_hold_by_local:
                Engine().connect_audio_transport(self._audio_transport)
            self.notification_center.post_notification("MediaStreamDidStart", self, TimestampedNotificationData())

    def send_dtmf(self, digit):
        with self._lock:
            if self.state != "ESTABLISHED":
                raise RuntimeError("AudioStream.send_dtmf() may only be called in the ESTABLISHED state")
            self._audio_transport.send_dtmf(digit)

    def _NH_RTPAudioStreamGotDTMF(self, audio_transport, data):
        self.notification_center.post_notification("AudioStreamGotDTMF", self,
                                                   NotificationData(timestamp=data.timestamp, digit=data.digit))

    def validate_update(self, remote_sdp, stream_index):
        with self._lock:
            # TODO: implement
            return True

    def update(self, local_sdp, remote_sdp, stream_index):
        with self._lock:
            new_direction = local_sdp.media[stream_index].get_direction()
            self._audio_transport.update_direction(new_direction)
            was_on_hold = self.on_hold_by_local
            self.on_hold_by_local = "recv" not in self._audio_transport.direction
            self.on_hold_by_remote = "send" not in self._audio_transport.direction
            if was_on_hold and not self.on_hold_by_local:
                Engine().connect_audio_transport(self._audio_transport)

    def end(self):
        with self._lock:
            if self.state != "ENDED":
                if self._audio_transport is not None:
                    self.notification_center.post_notification("MediaStreamWillEnd", self,
                                                               TimestampedNotificationData())
                    self._audio_transport.stop()
                    self.notification_center.remove_observer(self, sender=self._audio_transport)
                    self._audio_transport = None
                    self._rtp_transport = None
                    self.notification_center.post_notification("MediaStreamDidEnd", self,
                                                               TimestampedNotificationData())
                self.state = "ENDED"


class GreenAudioStream(GreenBase):
    implements(IMediaStream)

    klass = AudioStream

    def initialize(self, session):
        with self.linked_notifications(names=['MediaStreamDidInitialize', 'MediaStreamDidFail']) as q:
            self._obj.initialize(session)
            n = q.wait()
            if n.name == 'MediaStreamDidFail':
                raise SIPCoreError(n.data.reason)
            return n

    def start(self, local_sdp, remote_sdp, stream_index):
        with self.linked_notifications(names=['MediaStreamDidStart', 'MediaStreamDidFail']) as q:
            self._obj.start(local_sdp, remote_sdp, stream_index)
            n = q.wait()
            if n.name == 'MediaStreamDidFail':
                raise SIPCoreError(n.data.reason)
            return n

    def end(self):
        with self.linked_notifications(names=['MediaStreamDidStart', 'MediaStreamDidFail']) as q:
            self._obj.end()
            n = q.wait()
            if n.name == 'MediaStreamDidFail':
                raise SIPCoreError(n.data.reason)
            return n

