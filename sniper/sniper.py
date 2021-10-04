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
                conn.putheader("cookie", "__utma=200924205.1691920426.1626543786.1632230715.1632237101.548; __utmc=200924205; __utmz=200924205.1631088516.449.17.utmcsr=rolimons.com|utmccn=(referral)|utmcmd=referral|utmcct=/; _ga=GA1.2.1691920426.1626543786; _gcl_au=1.1.109854368.1626543786; .RBXIDCHECK=; gig_bootstrap_3_OsvmtBbTg6S_EUbwTPtbbmoihFY5ON6v6hbVrTbuqpBs7SyF_LQaJwtwKJ60sY1p=_gigya_ver4; GuestData=UserID=-646931058; __RequestVerificationToken=Ybk8ZYgYJEIZHN2MTBJMp62X2Ojx1HeybllkNeB20xArrnsxABW-k4tCqZmxNIjkZnQNzKtnwpvrZjpzW7BxQ8v18Sg1; RBXcb=RBXViralAcquisition=false&RBXSource=false&GoogleAnalytics=false; gig_canary=false; .ROBLOSECURITY=_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_D57E28F6FC3696904D57ACE566B128A16B67590A01837CA6F718E3684C514933D0B1A6485636CFE86A8D28ADE8E4C6F589776A3F51E9AABE1A738E8C7E0DE48A12C21FFA86BF679277A9AE39FF106FEEA8149880D29D04E418F3FEF8CD451659B23C5804107FC7F81CE25D8782B076B00614EE6B5E1C654E2556E2EA984BAEEF30170E0F2BE16D92C73E9C522F002AEB7411B014985C55EF964839FFD412A24674F3CA4BFD45BF44644235A68BBC821124528A175E7877079C935ACD8C618CAAEF92593AF0ECB37E303BC71647CAC524F32B7F674063590942196160AAA56219E9E84FADA7F7CFA2BB45BFA4ACEE3B44A6C032DFA0BA2BDC7BECD84FA296735DCE9FD267DF11D0A76E7FD08909D8DC26716CC240DC9859D96ADC719BD93526C993A81F9F62F0AFF037988072650F654418B445E76D6290729E78B4B6102DCCEDF6C648C8A78728410380DBDD49B5E71898117B1C; .RBXID=_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_eyJhbGciOiJIUzI1NiJ9.eyJqdGkiOiIyMjMyMTJjMi1jZTIwLTRmYzQtODYzYi00ZGNkNGFlOTUzMDkiLCJzdWIiOjYzMzU1OTQzNX0.xYpzATbTrvf0zU2k0BorgDweyzC-crN-lJcaiyTFfmE; RBXEventTrackerV2=CreateDate=9/21/2021 12:18:09 PM&rbxid=40155045&browserid=117925662064; RBXSessionTracker=sessionid=8d292dd7-515e-4cb9-b432-55c7be5b8456; lightstep_guid%2FWeb=344d73b84bfe0915; lightstep_session_id=6b8bc5d72655560c; _gid=GA1.2.1080856368.1632256434; gig_canary_ver=12426-3-27204315; rbx-ip2=")
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
