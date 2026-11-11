"""Standard-library live API acceptance test; restores the previous fault settings."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import http.cookiejar
import json
import time
import urllib.error
import urllib.request


class Session:
    def __init__(self, base):
        self.base = base.rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = self.call('GET','/api/session')[1]['csrf_token']

    def call(self, method, path, data=None):
        headers={'Accept':'application/json'}
        body=None
        if data is not None:
            body=json.dumps(data).encode()
            headers.update({'Content-Type':'application/json','X-CSRF-Token':self.csrf})
        request=urllib.request.Request(self.base+path,data=body,headers=headers,method=method)
        try:
            with self.opener.open(request,timeout=20) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            return error.code,json.load(error)

    def login(self):
        status,data=self.call('POST','/api/login',{'username':'demo_user','password':'demo_pass','traffic_type':'synthetic'})
        assert status==200,(status,data)
        self.csrf=data['csrf_token']

    def post(self,path,data=None):
        return self.call('POST',path,{} if data is None else data)


def run(base):
    checks=[]
    client=Session(base)
    client.login()
    previous=client.call('GET','/api/lab/control')[1]['control']
    try:
        assert client.post('/api/lab/reset')[0]==200
        products=client.call('GET','/api/products')[1]['products']
        assert len(products)==2
        checks.append('login and products')
        client.post('/api/cart/items',{'product_id':'laptop'})
        client.post('/api/cart/items',{'product_id':'headphones'})
        cart=client.call('GET','/api/cart')[1]['cart']
        assert (cart['item_count'],cart['total_cents'])==(2,114800)
        assert client.post('/api/checkout')[0]==200
        assert client.call('GET','/api/cart')[1]['cart']['item_count']==0
        checks.append('normal checkout and server prices')

        client.post('/api/cart/items',{'product_id':'laptop'})
        client.post('/api/lab/control',{'mode':'slow','delay_ms':800})
        with ThreadPoolExecutor(max_workers=1) as pool:
            start=time.monotonic()
            pending=pool.submit(client.post,'/api/checkout')
            time.sleep(.15)
            health_start=time.monotonic()
            with urllib.request.urlopen(base.rstrip('/')+'/healthz',timeout=3) as response:
                assert response.status==200
            health_elapsed=time.monotonic()-health_start
            status,data=pending.result(timeout=10)
        slow_elapsed=time.monotonic()-start
        assert status==200 and slow_elapsed>=.7 and health_elapsed<.6,(slow_elapsed,health_elapsed)
        checks.append('slow checkout with responsive independent health request')

        client.post('/api/lab/control',{'mode':'error'})
        client.post('/api/cart/items',{'product_id':'headphones'})
        status,data=client.post('/api/checkout')
        assert status==500 and data['error']['code']=='SIMULATED_CHECKOUT_ERROR'
        assert client.call('GET','/api/cart')[1]['cart']['item_count']==1
        checks.append('HTTP 500 preserves cart')
        client.post('/api/lab/control',{'mode':'recovery'})
        status,data=client.post('/api/checkout')
        assert status==200 and data['order']['scenario']=='recovery'
        checks.append('recovery checkout')

        unprotected=urllib.request.Request(base.rstrip('/')+'/api/cart/items',data=b'{}',headers={'Content-Type':'application/json'},method='POST')
        try:
            client.opener.open(unprotected,timeout=5)
            raise AssertionError('Mutation without CSRF unexpectedly succeeded')
        except urllib.error.HTTPError as error:
            assert error.code==400
            assert json.load(error)['error']['code']=='INVALID_CSRF'
        checks.append('CSRF rejection')
        assert client.call('GET','/api/session')[1]['traffic_type']=='synthetic'
        checks.append('synthetic session label')
    finally:
        status,data=client.post('/api/lab/reset')
        assert status==200 and data['control']['mode']=='normal' and data['cart']['item_count']==0
        status,data=client.post('/api/lab/control',{'mode':previous['mode'],'delay_ms':previous['delay_ms']})
        assert status==200 and data['control']['mode']==previous['mode'] and data['control']['delay_ms']==previous['delay_ms']
        assert client.post('/api/logout')[0]==200
    assert client.call('GET','/api/cart')[0]==401
    checks.append('reset, restore fault settings and logout')
    return {'passed':True,'checks':checks,'slow_checkout_ms':round(slow_elapsed*1000),
            'health_during_slow_ms':round(health_elapsed*1000),'final_mode':previous['mode'],
            'restored_control':{'mode':previous['mode'],'delay_ms':previous['delay_ms']}}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',required=True)
    print(json.dumps(run(parser.parse_args().base_url),indent=2))
