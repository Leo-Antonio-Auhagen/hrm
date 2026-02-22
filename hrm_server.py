import nest_asyncio
import asyncio
from bleak import BleakClient, BleakScanner
from collections import deque
import numpy as np
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse
import uvicorn
import time
from datetime import timedelta
import atexit


nest_asyncio.apply()



rr_buffer = deque(maxlen=60)
hr_buffer = deque(maxlen=60)



# UUID for Heart Rate Measurement Characteristic
HRM_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

def parse_heart_rate(data):
    flag = data[0]
    hr_format = flag & 0x01
    sensor_contact = (flag >> 1) & 0x03
    energy_expended_present = (flag >> 3) & 0x01
    rr_interval_present = (flag >> 4) & 0x01

    index = 1
    if hr_format == 0:
        hr_value = data[index]
        index += 1
    else:
        hr_value = int.from_bytes(data[index:index+2], byteorder='little')
        index += 2

    if energy_expended_present:
        index += 2  # skip over energy expended

    rr_intervals = []
    if rr_interval_present:
        while index + 1 < len(data):
            rr = int.from_bytes(data[index:index+2], byteorder='little') / 1024  # in seconds
            rr_intervals.append(rr)
            index += 2

    return hr_value, rr_intervals


ectopics_count = 0
timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

log_file = open("./log_data/hr_log_" + timestamp + ".csv", "a", buffering=1)  # Line-buffered write
@atexit.register
def close_file():
    print("Exiting... closing file.")
    log_file.close()

async def handle_hr_data(sender, data):
    global ectopics_count, log_file
    hr, rr_intervals = parse_heart_rate(data)
    #print(f"HR: {hr} bpm")
    hr_buffer.append(hr)
    if rr_intervals:
        #print(f"RR Intervals: {[f'{rr:.3f}s' for rr in rr_intervals]}")
        rr_buffer.extend(rr_intervals)
        for rr in rr_intervals:
            if is_possible_ectopic(rr_buffer, rr):
                ectopics_count += 1
            
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            log_file.write(f"{timestamp}, {hr},{rr}\n")

def is_possible_ectopic(rr_buffer, last_rr, window_size=15):
    if len(rr_buffer) < window_size + 1:
        return False # could also be None
    recent = list(rr_buffer)[-window_size-1:-1] # select last window_size beats, but not the very last
    median_rr = np.median(recent)
    if median_rr - last_rr > 0.2 * median_rr:
        return True  # suspicious beat; https://pmc.ncbi.nlm.nih.gov/articles/PMC3232430/#sec2
    return False
    



recording_start_time = None
recording_start_localtime = None

async def connect_hrm():
    global recording_start_time, recording_start_localtime
    selected_device_name = ""
    iter_count = 0
    while True:
        iter_count += 1
        print("Scanning for BLE devices...")
        devices = await BleakScanner.discover(timeout=5)

        print(f"Trying to connect to '{selected_device_name}' ...")

        hrm_device = None
        for device in devices:
            if device.name != None:
                if selected_device_name == device.name: # or "HR" in device.name.upper() or "HEART" in device.name.upper():
                    hrm_device = device
                    break

        if hrm_device:  
            print(f"Connecting to {hrm_device.name} [{hrm_device.address}]...")
            async with BleakClient(hrm_device.address) as client:
                await client.start_notify(HRM_CHAR_UUID, handle_hr_data)
                print("Receiving heart rate data... Press Ctrl+C to stop.")
                recording_start_time = time.time()
                recording_start_localtime = time.localtime()
                try:
                    while True:
                        await asyncio.sleep(1)
                except KeyboardInterrupt:
                    print("Stopped by user.")
                finally:
                    await client.stop_notify(HRM_CHAR_UUID)
        else:
            # only show device selection after trying for 5 seconds
            if (iter_count > 5):
                print("No HRM device found. \nCtrl+C to stop, select from list or rescan [r]")
                print("Available devices:")
                devices_sorted = sorted([device for device in devices if device.name != None], key=lambda device: device.name)
                for idx, device in enumerate(devices_sorted):
                        print(f"[{idx}]: {device}")
                print("Select device: ... Then press Enter")
                selected_device_name = ""
                try:
                    user_input = input()

                    if user_input == "r": # retry
                        selected_device_name == ""
                        # wait for next try
                    else:
                        if not user_input.isdigit():
                            print("Please only enter the number!")
                        else:
                            selected_device_name = devices_sorted[int(user_input)].name
                            print(f"Selected '{selected_device_name}'")
                except asyncio.TimeoutError:
                    pass
                    # nothing to do, wait for next try
            else:
                print("No HRM device found. Retry in 1 seconds ... Press Ctr+C to cancel.")
                asyncio.wait(1)

#####################################################################
#       REST API                                                    #
#####################################################################

app = FastAPI()

@app.get("/buffer")
def get_buffers():
    return {
        "hr_buffer": list(hr_buffer),
        "rr_buffer": list(rr_buffer)
    }

@app.get("/stats")
def get_stats():
    if not recording_start_localtime:
        return None
    else:
        return {
        "start_time": time.strftime("%H:%M:%S [%d.%m.]", recording_start_localtime),
        "elapsed_time": timedelta(seconds=int(time.time() - recording_start_time)),
        "elapsed_time_lit": str(timedelta(seconds=int(time.time() - recording_start_time))),
        "num_ectopics": ectopics_count
    }


# setup index.html
index_html_content = None 

with open("index.html", "r", encoding="utf-8") as file:
    index_html_content = file.read()


# update index.html
async def refresh_html():
    global index_html_content
    while True:
        with open("index.html", "r", encoding="utf-8") as file:
            index_html_content = file.read()
        await asyncio.sleep(5)


@app.get("/", response_class=HTMLResponse)
def landing_page():
    return HTMLResponse(content=index_html_content)

# Run both BLE client and API server concurrently
async def main():
    task_hrm = asyncio.create_task(connect_hrm())
    config = uvicorn.Config(app=app, host="0.0.0.0", port=12080, log_level="info")
    server = uvicorn.Server(config)
    task_api = asyncio.create_task(server.serve())
    task_refresh_html = asyncio.create_task(refresh_html())

    await asyncio.gather(task_hrm, task_api, task_refresh_html)

if __name__ == "__main__":
    asyncio.run(main())