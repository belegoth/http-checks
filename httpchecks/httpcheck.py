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
import requests

from requests.packages.urllib3.exceptions import InsecureRequestWarning

from requests import Session

log = logging.getLogger(__name__)
# Monkey-patch.
gmonkey.patch_all(thread=False, select=False)


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
        self.domain_name = url.split("//")[-1].split("/")[0]
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
        except:
            # log.error("cannot open [%s]" % self.url)
            # log.exception("[%s] gave exception" % self.url)
            # return
            pass
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
        #log.warn("Cert expired %s days ago" % remaining.days)
        pass

    elif remaining < datetime.timedelta(days=buffer_days):
        # expires sooner than the buffer
        return False
    else:
        # everything is fine
        return True


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

    if req.response:
        log.debug("[%s] checking status code waiting: %s actual: %s", req.url, req.waiting_status_code,
                  req.response.status_code)
        return req.response.status_code in req.waiting_status_code

    return None


def check_response(req):
    resp_content = ""
    if req.response:
        resp_content = req.response.content
        return True
    #log.debug("[%s] response %s ", req.url, resp_content)
    return None


# return req.response
# return True

def check_cert(req):
    if not req.check_cert:
        return True

    return ssl_expires_in(req.domain_name)

    #
    #
    #
    #
    # j = json.loads(req.response.content)
    # for check in req.check_json:
    #     for k, v in check.items():
    #         path = parse(k)
    #         matches = path.find(j)
    #         if not matches:
    #             return False
    #         return matches[0].value == v


def notify_by_ovo(url, channel, username, description, icon_emoji):
    pass


def get_request(k, urlconf, callback=None, session=None):
    r = AsyncRequest(

        method=urlconf.get('method', 'GET'),
        timeout=urlconf.get('timeout', 5.0),
        url=urlconf['url'],
        allow_redirects=urlconf.get('allow_redirects', True),
        headers=urlconf.get('headers', None),
        verify=False,
        auth=(urlconf.get('username', None), urlconf.get('password', None)),
        session=session,
        callback=callback

    )
    r.name = k
    r.waiting_status_code = urlconf.get('status_code', None)
    r.check_cert = urlconf.get('check_cert', False)
    if not r.waiting_status_code:
        r.waiting_status_code = [200]

    return r


checks = [
    check_response,
    check_status_code,
    check_cert
]

ready = gevent.event.Event()
ready.clear()


sync_map = []



exit_code = 0


def main():
    global exit_code
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    sys.tracebacklimit = 0

    parser = argparse.ArgumentParser()
    parser.add_argument('--config', '-c', help='config file',
                        dest='config_file', default='check.yml')
    args = parser.parse_args()
    config = yaml.load(open(args.config_file))

    #logging.basicConfig(level=config['settings'].get('log_level', 'DEBUG').upper())
    logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%d.%m.%Y %I:%M:%S', level=config['settings'].get('log_level', 'CRITICAL').upper())

    rs = []

    for k, urlconf in config['urls'].items():

        if isinstance(urlconf, list):
            for c in urlconf:
                r = get_request(k, c)
                rs.append(r)

        else:
            r = get_request(k, urlconf)
            rs.append(r)

    reqs = map_requests(rs, size=config.get('pool_size', 10))

    for req in reqs:

        failed = False
        for check in checks:

            if not check(req):
                failed = True
                log.critical('%s: [%s] FAILED check - %s', req.name, req.url, check.__name__,  exc_info=False)

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

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
