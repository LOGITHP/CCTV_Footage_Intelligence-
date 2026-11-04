import os
from dotenv import load_dotenv
load_dotenv('.env')

from video_intelligence.infrastructure.postgres import PostgresStore

db_url = os.environ.get('DATABASE_URL')
if db_url:
    store = PostgresStore(db_url)
    cams = store.list_cameras()
    print("Initial cameras in DB:", [c['camera_id'] for c in cams])
    for c in cams:
        if c['camera_id'] == 'cam-gate-02':
            deleted = store.delete_camera(c['camera_id'])
            print(f"Deleted {c['camera_id']}: {deleted}")
    print("Remaining cameras in DB:", [c['camera_id'] for c in store.list_cameras()])
else:
    print("DATABASE_URL not set.")

