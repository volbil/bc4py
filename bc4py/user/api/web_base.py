from aiohttp import web
import json
from logging import getLogger

log = getLogger('bc4py')

# Content-Type
CONTENT_TYPE = 'Content-Type'
CONTENT_TYPE_HTML = {'Content-Type': 'text/html'}
CONTENT_TYPE_JSON = {'Content-Type': 'application/json'}


async def content_type_json_check(request):
    if request.content_type != 'application/json':
        raise TypeError('Content-Type is application/json,'
                        ' not {}'.format(request.content_type))
    else:
        try:
            return await request.json()
        except json.JSONDecodeError:
            # POST method check, but No body found
            body = await request.text()
            log.error("content_type_json_check() body={}".format(body))


def json_res(data, indent=4):
    return web.Response(
        text=json.dumps(data, indent=indent),
        content_type='application/json')


def error_res(errors=None):
    if errors is None:
        import traceback
        errors = str(traceback.format_exc())
    log.info("API error:\n{}".format(errors))
    s = errors.split("\n")
    simple_msg = None
    while not simple_msg:
        simple_msg = s.pop(-1)
    return web.Response(text=simple_msg+'\n', status=400)
