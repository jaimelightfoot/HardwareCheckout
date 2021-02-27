from .models import DeviceQueue, DeviceType, User, UserQueue, TwitchStream
from .webutil import Blueprint, UserBaseHandler, Timer, make_session
from tornado_sqlalchemy import as_future
from sqlalchemy import func, or_
from tornado import locks

main = Blueprint()


@main.route("/", name="main")
class MainHandler(UserBaseHandler):
    timer = None
    RWTerminals = []
    ROTerminals = []
    queues = []
    pictures = []
    tstreams = []
    lock = locks.Lock()

    async def get(self):
        """
        Home path for the site

        :return:
        """
        # default values
        terminals = self.ROTerminals
        show_streams = True
        devices = list()
        queues = list()
        tstreams = self.tstreams
        pictures = self.pictures
        adminuser = False

        # If no background queue update thread as started, start it
        if self.timer is None:
            async with self.lock:  # we don't want the overhead of getting the lock EVERY loop, so this should be fine
                if self.timer is None:
                    print("Scheduling")
                    self.__class__.timer = Timer(self.updateQueues, timeout=5)

        # check if use is logged in
        if self.current_user:
            with self.make_session() as session:
                # check if the user is an admin
                try:
                    current_user = await as_future(
                        session.query(User).filter_by(id=self.current_user).one
                    )
                except Exception:
                    pass
                else:
                    if current_user.has_roles("Admin"):
                        adminuser = True
                        terminals = self.RWTerminals
                        show_streams = False
                        devices = []
                    else:
                        # Get any devices the user may own.
                        devices = await current_user.get_owned_devices_async(session)
                        devices = [
                            {"name": a[0], "sshAddr": a[1], "webUrl": a[2]}
                            for a in devices
                        ]

            # get a listing of all the queues available
            # Make a copy of the list because we are iterating through it
            tqueues = self.queues
            queues = [
                {"id": i[0], "name": i[1], "image": i[3], "size": i[3]} for i in tqueues
            ]

        self.render(
            "index.html",
            devices=devices,
            tstreams=tstreams,
            queues=queues,
            show_streams=show_streams,
            terminals=terminals,
            pictures=pictures,
            adminuser=adminuser,
        )

    @classmethod
    async def updateQueues(cls):
        async with cls.lock:
            with make_session() as session:
                cls.RWTerminals = await as_future(
                    session.query(User.name, DeviceQueue.roUrl, DeviceQueue.webUrl, DeviceQueue.sshAddr)
                    .join(User.deviceQueueEntry)
                    .filter_by(state="in-use")
                    .all
                )
                cls.queues = await as_future(
                    session.query(
                        DeviceType.id,
                        DeviceType.name,
                        DeviceType.image_path,
                        func.count(UserQueue.userId),
                    )
                    .select_from(DeviceType)
                    .filter_by(enabled=1)
                    .join(UserQueue, isouter=True)
                    .group_by(DeviceType.id, DeviceType.name)
                    .all
                )
                cls.ROTerminals = await as_future(
                    session.query(User.name, DeviceQueue.roUrl)
                    .join(User.deviceQueueEntry)
                    .filter(DeviceQueue.state == "in-use")
                    .filter(User.ctf == 0)
                    .all
                )
                cls.tstreams = await as_future(session.query(TwitchStream.name).all)
                cls.pictures = await as_future(
                    session.query(
                        DeviceType.image_path, DeviceType.name, DeviceType.enabled
                    ).all
                )
