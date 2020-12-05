"""
A brief guide to all the device states:
  * ready  - device is ready to be used but not in queue
  * in-queue - device is queued up to be used
  * in-use - device is currently being used
  * want-deprovision - server wants the device to deprovision itself
  * is-deprovisioned - device has deprovisioned itself
  * want-provision - server wants the device to provision itself
  * is-provisioned - device has provisioned itself

State transition guide:

ready -> in-queue -> in-use -> want-deprovision -> is-deprovisioned -> want-provision -> is-provisioned -> ready
             \                        /^
              ------------------------

Other states
  * provision-failed - provision script failed (non-zero exit code)
  * deprovision-failed - deprovision script failed (non-zero exit code)
  * disabled - device disabled by admin
"""

from base64 import b64decode
from datetime import datetime, timedelta
from functools import wraps, partial

from tornado.web import authenticated
from tornado.escape import json_decode
from tornado.ioloop import IOLoop
from sqlalchemy.orm.exc import NoResultFound
from tornado_sqlalchemy import as_future
from werkzeug.security import check_password_hash

from .models import DeviceQueue, DeviceType, UserQueue, User
from .webutil import Blueprint, UserBaseHandler, DeviceWSHandler, Timer, make_session
from .queue import QueueWSHandler, on_user_assigned_device, on_user_deallocated_device

device = Blueprint()


@device.route("/hook")
class DeviceStateHandler(UserBaseHandler):
    __timer = None
    __timer_dict = dict()

    def get(self):
        # send them home
        self.redirect(self.reverse_url("main"))
        return

    async def post(self):
        # if there is no timer, start one
        if self.__class__.__timer is None:
            self.__class__.__timer = Timer(self.__class__.__callback, True)
            self.__class__.__timer.start()

        try:
            data = json_decode(self.request.body)
        except Exception:
            return

        message_type = data.get("type", None)
        entity = data.get("entity_id", None)
        user_data = data.get("userdata", None)
        params = data.get("params", None)
        if None in [message_type, entity, user_data, params]:
            return

        if message_type == "session_register":
            await self.handle_session_register(entity, user_data, params)

        elif message_type == "session_join":
            await self.handle_session_join(entity, user_data, params)

        elif message_type == "session_close":
            await self.handle_session_close(entity, user_data, params)

    async def handle_session_register(self, entity, user_data, params):
        # check the user data to see if it is valid
        try:
            username, password = b64decode(user_data).decode().split("=")
        except Exception:
            return

        # Checks the db to see if this is valid user data
        with self.make_session() as session:
            try:
                device = await as_future(
                    session.query(DeviceQueue).filter_by(name=username).one
                )
            except Exception:
                return

            if not check_password_hash(device.password, password):
                return

            # register entity id with db and update ssh/web/webro info
            ssh_fmt = params.get("ssh_cmd_fmt", None)
            web_fmt = params.get("web_url_fmt", None)
            stoken = params.get("stoken", None)
            stoken_ro = params.get("stoken_ro", None)
            if not ssh_fmt or not web_fmt or not stoken or not stoken_ro:
                return

            device.sshAddr = ssh_fmt % stoken
            device.webUrl = web_fmt % stoken
            device.roUrl = web_fmt % stoken_ro
            device.state = "provisioned"
            device.entity_id = entity
            session.add(device)

            deviceID = device.id
            deviceType = device.type

        # await DeviceStateHandler.check_for_new_owner(deviceID, deviceType)

    async def handle_session_join(self, entity, user_data, params):
        # Check if it is a read only session. We only care about R/W sessions
        if params.get("readonly", True):
            return

        with make_session() as session:
            try:
                device = await as_future(
                    session.query(DeviceQueue).filter_by(entity_id=entity).one
                )
            except Exception:
                return

            if device.state != "in-use":
                device.state = "in-use"
                session.add(device)
                await self.device_in_use(device.id)

    async def handle_session_close(self, entity, user_data, params):
        # Technically there could be a race condition where the close message comes after the next start message.
        # In that case it is ok since the entity ID should have been updated before then.
        with make_session() as session:
            try:
                device = await as_future(
                    session.query(DeviceQueue).filter_by(entity_id=entity).one
                )
            except Exception:
                return

            device.state = "deprovisioned"
            device.sshAddr = None
            device.webUrl = None
            device.roUrl = None
            device.entity_id = None
            device.owner = None
            # TODO should I null more fields?
            session.add(device)

    @staticmethod
    async def deprovision_device(deviceID):
        raise NotImplementedError("TODO: this will tell the watcher to kill a session")

    @staticmethod
    async def check_for_new_owner(deviceID, deviceType):
        with make_session() as session:
            next_user = await as_future(
                session.query(UserQueue)
                .filter_by(type=deviceType)
                .order_by(UserQueue.id)
                .first
            )
            if next_user:
                session.delete(next_user)
                session.commit()
                return await DeviceStateHandler.device_in_queue(
                    deviceID, next_user.userId
                )

    @staticmethod
    async def device_in_queue(deviceID, next_user):
        with make_session() as session:
            device = await as_future(
                session.query(DeviceQueue).filter_by(id=deviceID).first
            )
            device.state = "in-queue"  # Set this to in queue so the callback doesn't try to hand it out again
            device.owner = next_user
            session.add(device)

        timer = Timer(
            DeviceStateHandler.return_device,
            repeat=False,
            timeout=1800,
            args=[deviceID, "queue_timeout"],
        )
        timer.start()
        try:
            DeviceStateHandler.push_timer(deviceID, timer)
        except KeyError:
            old_timer = DeviceStateHandler.pop_timer(deviceID)
            old_timer.stop()
            del old_timer
            DeviceStateHandler.push_timer(deviceID, timer)

        with make_session() as session:
            device = await as_future(
                session.query(DeviceQueue).filter_by(id=deviceID).first
            )
            userID = await as_future(
                session.query(User.id).filter_by(id=next_user).first
            )
            on_user_assigned_device(userID[0], device)

    @staticmethod
    async def return_device(deviceID, reason):
        await ControllerHandler.restart_device(deviceID)
        with make_session() as session:
            userID = await as_future(
                session.query(DeviceQueue.owner).filter_by(id=deviceID).one
            )
        on_user_deallocated_device(userID, deviceID, reason)

    @staticmethod
    async def device_in_use(deviceID):
        timer = Timer(
            DeviceStateHandler.return_device,
            repeat=False,
            timeout=1800,
            args=[deviceID, "normal_timeout"],
        )
        timer.start()
        try:
            DeviceStateHandler.push_timer(deviceID, timer)
        except KeyError:
            old_timer = DeviceStateHandler.pop_timer(deviceID)
            old_timer.stop()
            del old_timer
            DeviceStateHandler.push_timer(deviceID, timer)

    @staticmethod
    async def killSession(deviceID):
        try:
            timer = DeviceStateHandler.pop_timer(deviceID)
            timer.stop()
            del timer
        except KeyError:
            pass
        await DeviceStateHandler.return_device(deviceID, "killed")

    @classmethod
    def push_timer(cls, deviceID, timer):
        """
        not worth asyncing
        """
        if cls.__timer_dict.get(deviceID, False):
            raise KeyError("device timer already registered")
        cls.__timer_dict[deviceID] = timer

    @classmethod
    def pop_timer(cls, deviceID):
        """
        Not worth asyncing
        """
        return cls.__timer_dict.pop(deviceID)

    @staticmethod
    async def __callback():
        with make_session() as session:
            for deviceID, deviceType in await as_future(
                session.query(DeviceQueue.id, DeviceQueue.type)
                .filter_by(state="provisioned")
                .all
            ):
                await DeviceStateHandler.check_for_new_owner(deviceID, deviceType)


@device.route("/controller")
class ControllerHandler(DeviceWSHandler):
    __listeners = {}

    async def open(self):
        # TODO : change this check to require a controller user name and password
        # not just any device.
        self.device = await self.check_authentication()
        # self.__listeners[self.device] = self

    async def on_message(self, message):
        try:
            data = json_decode(message)
        except Exception:
            return

        msg_type = data.get("type", None)
        params = data.get("params", None)

        if not msg_type:
            return
        elif msg_type == "register":
            if not params:
                return
            try:
                self.__listeners[params] = self
            except Exception:
                return

    async def close(self):
        self.__listeners.pop(self.device)

    @classmethod
    async def restart_device(cls, device):
        with make_session() as session:
            deviceName = await as_future(
                session.query(DeviceQueue.name).filter_by(id=device).one
            )
        try:
            await cls.__listeners[deviceName[0]].write_message(
                {"type": "restart", "params": deviceName[0]}
            )
        except Exception:
            return False
        return True