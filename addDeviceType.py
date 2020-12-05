#!/usr/bin/env python3
from HardwareCheckout.models import DeviceType
from HardwareCheckout.config import db_path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("-t", "--type", help="Device type to add", required=True)
parser.add_argument("-p", "--picture", help="name of the picture to add", required=True)
args = parser.parse_args()


session = sessionmaker(bind=create_engine(db_path))
s = session()
s.add(DeviceType(name=args.type, enabled=1, image_path=args.picture))
s.commit()