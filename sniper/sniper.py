import os.path
import json
import threading
import requests
import http.client
import re
import time
import ctypes
from httpstuff import ProxyPool, AlwaysAliveConnection
from itertools import cycle

xsrf_token = None
refresh_count = 0
target = None
target_updated = 0
target_lock = threading.Lock()

PRODUCT_ID_RE = re.compile(r'data\-item\-id="(\d+)"')
PRICE_RE = re.compile(r'data\-expected\-price="(\d+)"')
SELLER_ID_RE = re.compile(r'data\-expected\-seller-id="(\d+)"')
USERASSET_ID_RE = re.compile(r'data\-lowest\-private\-sale\-userasset\-id="(\d+)"')

def parse_item_page(data):
    product_id = int(PRODUCT_ID_RE.search(data).group(1))
    price = int(PRICE_RE.search(data).group(1))
    seller_id = int(SELLER_ID_RE.search(data).group(1))
    userasset_id = int(USERASSET_ID_RE.search(data).group(1))
    return product_id, price, seller_id, userasset_id

# load cookie
try:
    with open("cookie.txt") as fp:
        COOKIE = fp.read().strip()
except FileNotFoundError:
    exit("The cookie.txt file doesn't exist, or is empty.")

# load config
try:
    with open("config.json") as fp:
        config_data = json.load(fp)
        PRICE_CHECK_THREADS = int(config_data["price_check_threads"])
        XSRF_REFRESH_INTERVAL = float(config_data["xsrf_refresh_interval"])
        TARGET_ASSETS = config_data["targets"]
        del config_data
except FileNotFoundError:
    exit("The config.json file doesn't exist, or is corrupted.")

# prevent mistakes from happening
if any([price > 2000000 for asset_id, price in TARGET_ASSETS]):
    exit("You put the price threshold above 500,000 R$ for one of your targets, are you sure about this?")

# load proxies
proxy_pool = ProxyPool(PRICE_CHECK_THREADS + 1)
try:
    with open("proxies.txt") as f:
        proxy_pool.load(f.read().splitlines())
except FileNotFoundError:
    exit("The proxies.txt file was not found")

target_iter = cycle([
    (
        requests.get(f"https://www.roblox.com/catalog/{asset_id}/--").url \
            .replace("https://www.roblox.com", ""),
        price
    )
    for asset_id, price in TARGET_ASSETS
])

class StatUpdater(threading.Thread):
    def __init__(self, refresh_interval):
        super().__init__()
        self.refresh_interval = refresh_interval

    def run(self):
        while 1:
            time.sleep(self.refresh_interval)
            ctypes.windll.kernel32.SetConsoleTitleW(f"refresh count: {refresh_count}")

class XsrfUpdateThread(threading.Thread):
    def __init__(self, refresh_interval):
        super().__init__()
        self.refresh_interval = refresh_interval

    def run(self):
        req = requests.Session()
        global xsrf_token

        while 1:
            try:
                req.cookies['.ROBLOSECURITY'] = COOKIE
                r = req.post('https://auth.roblox.com/v2/login')
                new_xsrf = r.headers['X-CSRF-TOKEN']

                if new_xsrf != xsrf_token:
                    xsrf_token = new_xsrf
                    print("updated xsrf:", new_xsrf)

                time.sleep(self.refresh_interval)
            except Exception as err:
                print("xsrf update error:", err, type(err))

class BuyThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.conn = AlwaysAliveConnection("economy.roblox.com", refresh_interval=5)
        self.event = threading.Event()

    def run(self):
        while True:
            self.event.wait()
            self.event.clear()

            try:
                starter = time.process_time()
                conn = self.conn.get()
                conn.request(
                    method="POST",
                    url=f"/v1/purchases/products/{target[0]}",
                    body='{"expectedCurrency":1,"expectedPrice":%d,"expectedSellerId":%d,"userAssetId":%d}' % (target[1], target[2], target[3]),
                    headers={"Content-Type": "application/json", "Cookie": ".ROBLOSECURITY=%s" % COOKIE, "X-CSRF-TOKEN": xsrf_token}
                )
                ender = time.process_time()
                resp = conn.getresponse()
                data = json.loads(resp.read())
                print(float(time.perf_counter()))
                wow = []
                wow.append(data)
                price = target[1]
                seller = target[2]
                product = data['productId']
                if data['purchased'] == True:
                    webhook = 'put success webhook here'
                    data = {
                          'embeds':[{
                              'author': {
                                  'name': f'Successful Snipe: {product}',
                                  'url': f'https://www.roblox.com/catalog/{product}'
                                  },
                              'color': int('000000',16),
                              'fields': [
                                  {'name': '\u200b','value': f'```\nID: {product}\nPrice: {price}\nTime: {time.time()-target_updated}```','inline':False},
                                  {'name': '\u200b','value': f'```\nData: {data}```','inline':False},
                              ],
                              'thumbnail': {
                                  'url': 'https://cdn.discordapp.com/avatars/530030136637259787/31304a37dc9a486ce68a7316a4b5c48b.png?size=1024',
                                  }
                          }]
                        }
                    r = requests.post(webhook,json=data).text
                elif data['purchased'] == False:
                    webhook = 'put failed webhook here'

                    data = {
                          'embeds':[{
                              'author': {
                                  'name': f'Missed Snipe: {product}',
                                  'url': f'https://www.roblox.com/catalog/{product}'
                                  },
                              'color': int('000000',16),
                              'fields': [
                                  {'name': '\u200b','value': f'ID: {product}\nPrice: {price}\nTime: {time.time()-target_updated}','inline':False},
                                  {'name': '\u200b','value': f'```\nData: {data}```','inline':False},
                              ],
                              'thumbnail': {
                                  'url': 'https://cdn.discordapp.com/avatars/530030136637259787/31304a37dc9a486ce68a7316a4b5c48b.png?size=1024',
                                  }
                          }]
                        }
                    r = requests.post(webhook,json=data).text

            except Exception as err:
                print(f"failed to buy {target} due to error: {err} {type(err)}")

class PriceCheckThread(threading.Thread):
    def __init__(self, buy_threads):
        super().__init__()
        self.buy_threads = buy_threads

    def run(self):
        global target, target_updated, refresh_count

        while True:
            asset_url, price_threshold = next(target_iter)
            proxy = proxy_pool.get()

            try:
                start_time = time.time()
                conn = proxy.get_connection("www.roblox.com")
                conn.putrequest("GET", asset_url, True, True)
                conn.putheader("Host", "www.roblox.com")
                conn.putheader("sec-ch-ua", '"Google Chrome";v="93", " Not;A Brand";v="99", "Chromium";v="93"')
                conn.putheader("accept-language", "en-GB,en-US;q=0.9,en;q=0.8")
                conn.putheader("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.82 Safari/537.36")
                conn.putheader("cookie", "GuestData=UserID=-1623320351; RBXcb=RBXViralAcquisition=false&RBXSource=false&GoogleAnalytics=false; .ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_485CAFCE0DE9CB797F1DFDEA3DBA6A65429E6B816F480BD6EA4C36CA23DA2DEB5E563A4DDBBC5053A7818CB8D656E9CA2F6C6E63B592C72F3B8588F7BCC655587DABCAA8B8D6FDEA523F18AC4D223B4CBF60669054D2FC6934E116B517715DDCB80347EA16266B7BAC34CF7263E6ECF4AF14792EFF6D79A63987738F2597C919C6E4C4F102412FCD98D51F8E1E38F8FC466B6710DC2C0EEE574403008F0FB18CCC101603446AB9ADED8AA2FA665C125E9F5FE6D1B0679FFC28083B10FBE65E782D02C7BC5A8DCBC99E07B4EFC60015BCC2A0094CEB07E25E0A6AA7E85FF5D1353EB2712323E28AB427033AAE09D9254A7CBAEB71AD3D749AF8DFE30463171A6D7FD8498F4FD68E7D87E4D1C0391BF870C0F89B73313CE2F01BBAF2D6B8B66EA1C02AB4160258496F211F3BEB3F96B8D4AFD419ECB89D5B8D908694247B499B5414B9D25C986B3802EBC31F3ADF9EC05AECD02AC5; .RBXID=_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_eyJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJkY2U2OTRmOS1jNGNmLTRjMTQtYWQzOS00MzkxNTMxNjkzYTIiLCJzdWIiOjE1ODExNzYxNTl9.n5jVwJkoP177mWYheoBdhuPoEnmriZ4fnkRAsYJyWCI; RBXSessionTracker=sessionid=a4469e24-9faa-444b-8795-0cc4104843ef; RBXEventTrackerV2=CreateDate=10/5/2021 7:09:24 PM&rbxid=2948011739&browserid=118829515198")
                conn.endheaders()
                resp = conn.getresponse()
                data = resp.read()

                if len(data) < 1000:
                    raise Exception("Weird response")
                reseller = parse_item_page(data.decode("UTF-8"))
                if reseller[1] > 0 and reseller[1] <= price_threshold:
                    with target_lock:
                        if target != reseller and start_time > target_updated:
                            target = reseller
                            target_updated = time.time()
                            for t in buy_threads: t.event.set()
                            print("target set:", target)


                refresh_count += 1
                proxy_pool.put(proxy)
            except:
                pass

# start threads
stat_thread = StatUpdater(1)
stat_thread.start()
xsrf_thread = XsrfUpdateThread(XSRF_REFRESH_INTERVAL)
xsrf_thread.start()

buy_threads = [BuyThread() for _ in range(1)]
for t in buy_threads: t.start()

pc_threads = [PriceCheckThread(buy_threads) for _ in range(PRICE_CHECK_THREADS)]
for t in pc_threads: t.start()

print("running 100%!")
