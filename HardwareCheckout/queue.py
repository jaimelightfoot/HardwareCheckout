from tornado.web import authenticated
from tornado.websocket import WebSocketClosedError, WebSocketHandler
from tornado_sqlalchemy import as_future

from .models import DeviceType, UserQueue, User
from .webutil import Blueprint, Waiters, UserBaseHandler, Timer, make_session

queue = Blueprint()


def on_user_assigned_device(userId:int, device_id:str, device_type:str, device_name:str, device_ssh:str, device_url:str):
    message = {"type": "queue_shrink", "queue": device_type}
    QueueWSHandler.waiters.broadcast(message)

    device_info = {
        "id": device_id,
        "name": device_name,
        "sshAddr": device_ssh,
        "webUrl": device_url,
    }

    message = {"type": "new_device", "device": device_info}
    return QueueWSHandler.waiters[userId].send(message)


def on_user_deallocated_device(userId, deviceID, reason="normal"):
    device_info = {"id": deviceID}
    message = {"type": "rm_device", "device": device_info, "reason": reason}
    return QueueWSHandler.waiters[userId].send(message)


@queue.route("/event")
class QueueWSHandler(UserBaseHandler, WebSocketHandler):
    waiters = Waiters()
    closed = {}

    def get_compression_options(self):
        # Non-None enables compression with default options.
        return {}

    async def open(self):
        if self.current_user:
            if self.closed.get(self.current_user, False):
                self.closed[self.current_user].stop()
                del self.closed[self.current_user]

            self.waiters[self.current_user].add(self)

            # send all devices, in case WS connecton was terminated then re-established
            # and a device was assigned in the meantime
            with self.make_session() as session:
                current_user = await as_future(
                    session.query(User).filter_by(id=self.current_user).one
                )

                devices = await current_user.get_owned_devices_async(session)
                devices = [
                    {"name": a[0], "sshAddr": a[1], "webUrl": a[2], "id": a[3]}
                    for a in devices
                ]
                self.write_message({"type": "all_devices", "devices": devices})
        else:
            # support updating queue numbers even if not logged in
            self.waiters[-1].add(self)

    # TODO: find out when this runs and how to make it async
    def send(self, message):
        """Callback for webutil.Waiters"""
        try:
            return self.write_message(message)
        except WebSocketClosedError:
            self.on_close()

    def on_message(self, message):
        """Callback from received WebSocket message."""
        print("Unhandled message received on websocket: {}".format(message))

    def on_close(self):
        if self.current_user:
            QueueWSHandler.waiters[self.current_user].remove(self)
            if 0 >= len(QueueWSHandler.waiters[self.current_user].bucket):
                t = Timer(
                    self.remove_user,
                    repeat=False,
                    timeout=(60 * 15),
                    args=(self.current_user,),
                )
                self.closed[self.current_user] = t
        else:
            QueueWSHandler.waiters[-1].remove(self)

    @classmethod
    async def remove_user(cls, user):
        with make_session() as session:
            queueEntry = await as_future(
                session.query(UserQueue).filter_by(userId=user).delete
            )


# @queue.route('/')
# class ListAllQueuesHandler(SessionMixin, RequestHandler):
#     async def get(self):
#         queues = await DeviceType.get_queues_async(self.session)
#         self.write({'result': [{'id': id, 'name': name, 'size': size} for id, name, size in queues]})


@queue.route(r"/(\d+)")
class SingleQueueHandler(UserBaseHandler):
    @authenticated
    async def get(self, id):
        with self.make_session() as session:
            current_user = await as_future(
                session.query(User).filter_by(id=self.current_user).one
            )

            if not current_user.has_roles("Admin"):
                self.redirect(self.reverse_url("ROTerminals"))
                return
        try:
            id = int(id)
        except ValueError:
            self.render("error.html", error="Invalid Queue")
            return

        with self.make_session() as session:
            names = await as_future(
                session.query(User.name)
                .select_from(UserQueue)
                .join(User)
                .filter(UserQueue.type == id)
                .order_by(UserQueue.id)
                .all
            )
        self.write({"result": [{"name": name} for name in names]})
        # self.redirect(self.reverse_url("main"))

    @authenticated
    async def post(self, id):
        try:
            id = int(id)
        except ValueError:
            self.render("error.html", error="Invalid Queue")
            return

        with self.make_session() as session:
            # Check if the user is already registered for a queue
            try:
                entry = await as_future(
                    session.query(UserQueue)
                    .filter_by(userId=self.current_user, type=id)
                    .first
                )
            except Exception:
                return self.render(
                    "error.html", error="Error while trying to join queue"
                )  # meh... someone else write a better error message

            if entry:
                return self.render(
                    "error.html", error="User already registered for this queue"
                )
            else:
                # quickly check if the queue is enabled
                try:
                    await as_future(
                        session.query(DeviceType.name).filter_by(id=id, enabled=1).one
                    )
                except Exception:
                    return self.render("error.html", error="Queue is disabled")

                # Add user to the queue
                newEntry = UserQueue(userId=self.current_user, type=id)
                session.add(newEntry)
                # Send them back to the front page
                self.redirect(self.reverse_url("main"))

            self.on_userqueue_insert(id)

            # # Check if someone is able to claim a device
            # current_user = await as_future(session.query(User).filter_by(id=self.current_user).one)
            # current_user.try_to_claim_device(session, id, on_user_assigned_device)

    @staticmethod
    def on_userqueue_insert(targetID):
        message = {"type": "queue_grow", "queue": targetID}
        QueueWSHandler.waiters.broadcast(message)
