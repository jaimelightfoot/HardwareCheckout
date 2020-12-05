import datetime
from tornado.web import RequestHandler, MissingArgumentError, authenticated
from tornado_sqlalchemy import as_future

# from . import db, socketio
from .models import User, UserQueue, DeviceQueue, DeviceType
from .webutil import Blueprint, UserBaseHandler

terms = Blueprint()


@terms.route("/terminals", name="ROTerminals")
class ROTerminalHandler(UserBaseHandler):
    @authenticated
    async def get(self):
        with self.make_session() as session:
            results = await as_future(
                session.query(User.name, DeviceQueue.roUrl)
                .join(User.deviceQueueEntry)
                .filter(DeviceQueue.state == "in-use")
                .filter(User.ctf == 0)
                .all
            )
        self.write({"urls": results})


@terms.route("/terminals/rw")
class RWTerminalHandler(UserBaseHandler):
    @authenticated
    async def get(self):
        with self.make_session() as session:
            current_user = await as_future(
                session.query(User).filter_by(id=self.current_user).one
            )

            if not current_user.has_roles("Admin"):
                self.redirect(self.reverse_url("ROTerminals"))
                return

            results = await as_future(
                session.query(
                    DeviceQueue.name,
                    DeviceQueue.webUrl,
                    DeviceQueue.sshAddr,
                    DeviceQueue.state,
                ).all
            )
        self.write({"urls": results})
