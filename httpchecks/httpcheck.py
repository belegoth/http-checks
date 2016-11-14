try:
    import gevent
    from gevent import monkey as gmonkey
    from gevent.pool import Pool
except ImportError:
    raise RuntimeError('Gevent is required.')

import yaml
import socket
import logging
import argparse
import sys
import os
import ssl
import datetime

log = logging.getLogger(__name__)
# Monkey-patch.
gmonkey.patch_all(thread=False, select=False)

from requests import Session



class AsyncRequest(object):
    """ Asynchronous request.

    Accept same parameters as ``Session.request`` and some additional:

    :param session: Session which will do request
    :param callback: Callback called on response.
                     Same as passing ``hooks={'response': callback}``
    """
    def __init__(self, method, url, **kwargs):
        #: Request method
        self.method = method
        #: URL to request
        self.url = url
        #: Associated ``Session``
        self.session = kwargs.pop('session', None)
        if self.session is None:
            self.session = Session()
        if self.session is None:
            self.session = Session()

        callback = kwargs.pop('callback', None)
        if callback:
            kwargs['hooks'] = {'response': callback}

        #: The rest arguments for ``Session.request``
        self.kwargs = kwargs
        #: Resulting ``Response``
        self.response = None
        self.error = None
        self.name = None
        self.waiting_status_code = None
        self.check_cert = None


    def send(self, **kwargs):
        """
        Prepares request based on parameter passed to constructor and optional ``kwargs```.
        Then sends request and saves response to :attr:`response`

        :returns: ``Response``
        """
        merged_kwargs = {}
        merged_kwargs.update(self.kwargs)
        merged_kwargs.update(kwargs)
        try:
            self.response = self.session.request(self.method,
                                              self.url, **merged_kwargs)
        except Exception as e:
            self.response = None
            self.error = e
            # log.exception("[%s] gave exception" % self.url)
            return
        return self.response

    def __repr__(self):
        return "<AsyncRequest %s>" % self.url

def ssl_expiry_datetime(hostname):
    ssl_date_fmt = r'%b %d %H:%M:%S %Y %Z'

    context = ssl.create_default_context()
    conn = context.wrap_socket(
        socket.socket(socket.AF_INET),
        server_hostname=hostname,
    )
    # 3 second timeout because Lambda has runtime limitations
    conn.settimeout(3.0)

    conn.connect((hostname, 443))
    ssl_info = conn.getpeercert()
    # parse the string from the certificate into a Python datetime object
    return datetime.datetime.strptime(ssl_info['notAfter'], ssl_date_fmt)

def ssl_valid_time_remaining(hostname):
    """Get the number of days left in a cert's lifetime."""
    expires = ssl_expiry_datetime(hostname)
    log.debug(
        "SSL cert for %s expires at %s",
        hostname, expires.isoformat()
    )
    return expires - datetime.datetime.utcnow()

def ssl_expires_in(hostname, buffer_days=14):
    """Check if `hostname` SSL cert expires is within `buffer_days`.

    Raises `AlreadyExpired` if the cert is past due
    """
    remaining = ssl_valid_time_remaining(hostname)

    # if the cert expires in less than two weeks, we should reissue it
    if remaining < datetime.timedelta(days=0):
        # cert has already expired - uhoh!
        log.warn("Cert expired %s days ago" % remaining.days)
    elif remaining < datetime.timedelta(days=buffer_days):
        # expires sooner than the buffer
        return True
    else:
        # everything is fine
        return False

def send(r, pool=None, stream=False, callback=None):
    """Sends the request object using the specified pool. If a pool isn't
    specified this method blocks. Pools are useful because you can specify size
    and can hence limit concurrency."""
    if pool != None:
        return pool.spawn(r.send, stream=stream)

    return gevent.spawn(r.send, stream=stream)



def map_requests(requests, stream=False, size=None):
    """Concurrently converts a list of Requests to Responses.

    :param requests: a collection of Request objects.
    :param stream: If True, the content will not be downloaded immediately.
    :param size: Specifies the number of requests to make at a time. If None, no throttling occurs.
    """

    requests = list(requests)

    pool = Pool(size) if size else None
    jobs = [send(r, pool, stream=stream) for r in requests]
    gevent.joinall(jobs)
    return [r for r in requests]



def check_status_code(req):
    log.debug("[%s] checking status code waiting: %s actual: %s", req.url, req.waiting_status_code, req.response.status_code)
    return req.response.status_code in req.waiting_status_code

def check_response(req):
    resp_content = ""
    if req.response:
        resp_content = req.response.content
    log.debug("[%s] response %s ", req.url, resp_content)
    return req.response



def notify_by_ovo(url, channel, username, description, icon_emoji):
    pass


def get_request(k, urlconf, callback=None, session=None):
    r = AsyncRequest(

            method = urlconf.get('method', 'GET'),
            timeout = urlconf.get('timeout', 5.0),
            url = urlconf['url'],
            allow_redirects = urlconf.get('allow_redirects', True),
            headers = urlconf.get('headers', None),
            data = urlconf.get('data', None),
            session = session,
            callback = callback
        )
    r.name = k
    r.waiting_status_code = urlconf.get('status_code', None)
    if not r.waiting_status_code:
        r.waiting_status_code = [200]

    return r

checks = [
    check_response,
    check_status_code
 ]


ready = gevent.event.Event()
ready.clear()

finished_jobs = 0
sync_map = []

def finished(result):
    global exit_code, finished_jobs
    finished_jobs += 1

    if finished_jobs == len(sync_map):
        log.info('all waiting jobs are completed.')
        ready.set()


class SessionedChecks(object):
    """
    this is just a container for tests with sessions
    """
    def __init__(self, name):
        self.name = name
        self.session = Session()
        self.steps = []
        self.step_num = 0

    def add(self, rs):
        self.steps.append(rs)


    def next(self):
        try:
            step = self.steps[self.step_num]
            self.step_num += 1
            return step
        except IndexError:
            return None

    def run_cb(self, *args, **kwargs):
        next_req = self.next()

        if next_req:
            for check in checks:
                self.result = check(args[0].request)
                if not self.result:
                    log.warn("[%s] test failed - step:%s - %s" % (self.name, self.step_num-2, self.steps[self.step_num-2].url))
                    finished(False)
                    return
            self.run(next_req)
        else:
            finished(True)

    def run(self, rs=None):

        if not rs:
            rs = self.next()

        p = gevent.spawn(rs.send, stream=None)
        p.request = rs
        p.link(self.run_cb)


exit_code = 0

def main():
    global exit_code

    parser = argparse.ArgumentParser()
    parser.add_argument('--config', '-c', help='config file',
                        dest='config_file', default='check.yml')
    args = parser.parse_args()
    config = yaml.load(open(args.config_file))

    logging.basicConfig(level=config['settings'].get('log_level', 'DEBUG').upper())

    rs = []

    for k, urlconf in config['urls'].items():

        # different sessions can run in parallel
        if isinstance(urlconf, list):
            for c in urlconf:
                r = get_request(k,c)
                rs.append(r)

        else:
            r = get_request(k, urlconf)
            rs.append(r)


    reqs = map_requests(rs, size=config.get('pool_size', 10))

    for req in reqs:
        elapsed = -1
        failed = False
        for check in checks:

            if not check(req):
                failed = True
                log.critical('[%s] FAILED check - %s', req.name, check.__name__)

                # ovo_config = config['settings'].get('ovo', None)
                # if ovo_config:
                #     notify_by_ovo(
                #         print("Send to OVO %s", req.name)
                #         # url = slack_config['url'],
                #         # channel  = slack_config['channel'],
                #         # username  = slack_config['username'],
                #         # description  = '[%s] FAILED check - %s - %s' % (req.name, req.url, check.__name__),
                #         # icon_emoji = slack_config['icon_emoji']
                #     )

                break
            else:
                log.info(req.name +" " + check.__name__ + " OK")

        if not failed:
            elapsed = req.response.elapsed.total_seconds()

        else:
            exit_code = 2
        log.info("[%s] completed in %s", req.name, elapsed)


    sys.exit(exit_code)

if __name__ == "__main__":
    main()
