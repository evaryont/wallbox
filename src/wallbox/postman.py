#!/usr/bin/env python
import dbus
import gobject
import dbus.mainloop.glib
import dbus.service
import utils
import facebook
import defs
import socket
import datetime
import re
import time
import urlparse
import urllib
import urllib2
import os
import logging

logging.basicConfig (level=defs.log_level)

class TimeoutError(Exception):
    def __init__ (self, value):
        self.value = value
    def __str__ (self):
        return repr (self.value)

class NoUpdateError(Exception):
    def __init__ (self, value):
        self.value = value
    def __str__ (self):
        return repr (self.value)

def touch(fname, times = None):
    fhandle = file(fname, 'a')
    try:
        os.utime(fname, times)
    finally:
        fhandle.close()

class Postman (dbus.service.Object):
    def __init__ (self, bus_name, bus_path):
        try:
            dbus.service.Object.__init__ (self, bus_name, bus_path)
        except KeyError:
            logging.debug ("DBus interface registration failed - other wallbox running somewhere")
            pass

    @dbus.service.method ("org.wallbox.PostmanInterface", in_signature='ssis', out_signature='')
    def setup (self, api_key, secret, notification_num, local_data_dir):
        path = local_data_dir + "/auth.pickle"
        self.fb = utils.restore_auth_status (path, api_key, secret)
        self.notification_num = notification_num
        self.uid = self.fb.uid
        self.current_status = None
        self.app_ids = []
        self.applications = {}
        self.user_ids = []
        self.users = []
        self.status = {}
        self.updated_timestamp = None
        self.refresh_interval = 60
        self.notification = []
        self.user_icons_dir = \
            "%s/user_icons" % local_data_dir
        self.app_icons_dir = \
            "%s/app_icons" % local_data_dir
        self.last_nid = 0
        logging.debug ("postman setup completed")

    def _dump_notification (self):
        dump_str = "notification_id, title_text, body_text, is_unread" + \
            ", is_hidden, href, app_id, sender_id\n"
        for n in self.notification:
            dump_str += "%s,%s,%s,%s,%s,%s,%s,%s\n" % \
                (n['notification_id'], n['title_text'], n['body_text'], \
                n['is_unread'], n['is_hidden'], n['href'], n['app_id'], n['sender_id'])
        logging.debug (dump_str)

    def _dump_comments (self):
        for post_id in self.status:
            logging.debug ("status: %s: %s" % (post_id, self.status[post_id]['message']))
            logging.debug ("comments:")
            for c in self.status[post_id]['comments']:
                logging.debug ("\t%s: %s" % (c['id'], c['text']))

    def _dump_status (self):
        logging.debug ("=== START === dump status")
        for skey in self.status:
            if self.status[skey].has_key ('message'):
                logging.debug ("status: %s" % self.status[skey]['message'])
            else:
                logging.debug ("ERROR status key: %s has no message" % skey)
                logging.debug ("detail:\n%s" % self.status[skey])
            if not self.status[skey].has_key ('notification_ids'):
                logging.debug ("NO notification_ids")
                continue
            nids_log = "nids: "
            for n in self.status[skey]['notification_ids']:
                nids_log += "%s, " % n
            logging.debug (nids_log)
        logging.debug ("\n=== END === dump status")

    def _query (self, query_str):
        for i in range (3):
            try:
                result = self.fb.fql.query (query_str)
                if result != None:
                    return result
            except:
                logging.debug ("URLError, Sleep 3 sec")
                time.sleep (3)
        return None
            
    def get_remote_current_status (self):
        logging.debug ("get remote current status")
        qstr = "SELECT uid, status_id, message, " + \
                "source FROM status WHERE uid='%s' LIMIT 1" % self.uid
        status = self._query (qstr)

        self.current_status = status[0]

    def _filter_none (self, items):
        for item in items:
            for k in item:
                if item[k] == None:
                    item[k] = ""

    def get_remote_notification (self):
        logging.debug ("get remote notification")
        notification = self._query \
            ("SELECT notification_id, title_text, body_text, is_unread" + \
            ", is_hidden, href, app_id, sender_id " + \
            "FROM notification WHERE recipient_id = '%s' LIMIT %s" % \
            (self.uid, self.notification_num))
        self._filter_none (notification)
        self.last_nid = notification[0]['notification_id']
        self.notification = notification
        self._dump_notification ()

    def get_remote_comments (self):
        logging.debug ("get remote comments (fast)")
        pattern_id = re.compile ("&id=(\d+)")
        new_status = {}

        matched_ns = [n for n in self.notification \
            if int (n['app_id']) == 19675640871 or int (n['app_id']) == 2309869772]

        if len (matched_ns) == 0:
            return

        post_ids = []
        subquery = []
        for n in matched_ns:
            m_id = pattern_id.search (n['href'])

            if m_id != None:
                uid = m_id.group (1)
                _str = "(source_id = %s AND permalink = '%s')" % (uid, n['href'])
                if _str not in subquery:
                    subquery.append (_str)

        substr = " OR ".join (subquery)
        qstr = "SELECT source_id, post_id, message, permalink FROM stream " + \
            "WHERE %s" % substr
        logging.debug ("status query: " + qstr)
        result = self._query (qstr)

        if len (result) < len (subquery):
            total_result = []
            logging.debug ("fast get stream failed, try slow get stream")
            for n in matched_ns:
                m_id = pattern_id.search (n['href'])

                if m_id != None:
                    uid = m_id.group (1)
                    logging.debug ("try href: %s" % n['href'])
                    for i in range (1, 4):
                        qstr = "SELECT source_id, post_id, message, permalink " + \
                                "FROM stream WHERE source_id = %s AND permalink = '%s' LIMIT %d" % (uid, n['href'], 10**i)
                        logging.debug (qstr)
                        result = self._query (qstr)
                        logging.debug (result)
                        if len (result) > 0:
                            total_result.append (result[0])
                            break
                            
            result = total_result

        for r in result:
            new_status[r['post_id']] = r
            nids = [n['notification_id'] for n in matched_ns if n['href'] == r['permalink']]
            new_status[r['post_id']]['notification_ids'] = nids
        
        post_ids = ["'%s'" % r['post_id'] for r in result]
        substr = ", ".join (post_ids)
        qstr = "SELECT fromid, text, post_id, id, time FROM comment WHERE post_id IN (%s)" % substr
        comment_result = self._query (qstr)
        for r in result:
            comment_list = [c for c in comment_result if c['post_id'] == r['post_id']]
            new_status[r['post_id']]['comments'] = comment_list

        self.status = new_status
        self._dump_status ()
        self._dump_comments ()

    def get_remote_icon (self, url, local_path):
        local_size = 0

        icon_name = os.path.basename \
            (urlparse.urlsplit (url).path)

        full_path = "%s/%s" % (local_path, icon_name)

        if os.path.exists (full_path) and os.path.isfile (full_path):
            #if modification time < 24hr, ignore update
            mtime = os.path.getmtime (full_path)
            if time.time() - mtime < 60 * 60 * 24: #24hr
                raise NoUpdateError ("mtime is %s, ignore update icon: %s" % (mtime, icon_name))

            local_size = os.path.getsize (full_path)

        try:
            logging.debug ("urlopen: %s" % url)
            remote_icon = urllib2.urlopen (url)
        except:
            raise TimeoutError ("urlopen timeout")

        info = remote_icon.info ()
        remote_size = int (info.get ("Content-Length"))
        remote_icon.close ()


        if remote_size != local_size or not os.path.exists (full_path):
            logging.debug ("size different remote/local: %d/%d, start dwonload icon" % (remote_size, local_size))
            try:
                urllib.urlretrieve (url, full_path)
                return icon_name
            except:
                raise TimeoutError ("urlretrieve timeout")
        else:
            logging.debug ("icon already exist: %s" % icon_name)
            touch (full_path)
            return icon_name

    def get_remote_users_icon (self):
        logging.debug ("get remote users icon")
        for n in self.notification:
            if not n['sender_id'] in self.user_ids:
                self.user_ids.append (n['sender_id'])
        for skey in self.status:
            if self.status[skey].has_key ('comments'):
                for c in self.status[skey]['comments']:
                    if not c['fromid'] in self.user_ids:
                        self.user_ids.append (c['fromid'])
        self.users = \
            self.fb.users.getInfo (self.user_ids, ["name", "pic_square"])

        self._filter_none (self.users)

        default_timeout = socket.getdefaulttimeout ()
        socket.setdefaulttimeout (GET_ICON_TIMEOUT)
        logging.debug ("socket timeout: %s" % socket.getdefaulttimeout ())
        timeout_count = 0
        for u in self.users:
            if (u['pic_square'] != None and len (u['pic_square']) > 0):
                if timeout_count < 3:
                    try:
                        u['pic_square_local'] = \
                            self.get_remote_icon (u['pic_square'], self.user_icons_dir)
                    except TimeoutError:
                        timeout_count += 1
                        logging.debug ("timeout")
                        u['pic_square_local'] = ""
                    except NoUpdateError:
                        icon_name = os.path.basename \
                            (urlparse.urlsplit (u['pic_square']).path)
                        logging.debug ("No need update: %s" % icon_name)
                        u['pic_square_local'] = icon_name
                        
                else:
                    logging.debug ("timeout 3 times")
                    u['pic_square_local'] = ""
            else:
                u['pic_square_local'] = ""
                
        socket.setdefaulttimeout (default_timeout)

    def get_remote_applications_icon (self):
        logging.debug ("get remote applications icon")
        for n in self.notification:
            if not str (n['app_id']) in self.app_ids:
                self.app_ids.append (str (n['app_id']))

        ids_str = ", ".join (self.app_ids)
        qstr = "SELECT icon_url, app_id FROM application WHERE app_id IN (%s)" % ids_str
        logging.debug ("qstr: %s" % qstr)
        apps = self.fb.fql.query (qstr)

        default_timeout = socket.getdefaulttimeout ()
        socket.setdefaulttimeout (GET_ICON_TIMEOUT)
        logging.debug ("socket timeout: %s" % socket.getdefaulttimeout ())
        timeout_count = 0
        for app in apps:
            if timeout_count < 3:
                try:
                    icon_name = self.get_remote_icon (app['icon_url'], self.app_icons_dir)
                except TimeoutError:
                    logging.debug ("timeout")
                    timeout_count += 1
                    icon_name = ""
                except NoUpdateError:
                    logging.debug ("No need update")
                    icon_name = os.path.basename \
                            (urlparse.urlsplit (app['icon_url']).path)
            else:
                icon_name = ""
            self.applications[int (app['app_id'])] = {'icon_name': icon_name}
        socket.setdefaulttimeout (default_timeout)

    def get_remote_last_nid (self):
        qstr = "SELECT notification_id FROM notification " + \
                "WHERE recipient_id = %d LIMIT 1" % int (self.uid)
        result = self._query (qstr)
        if len (result) > 0:
            return result[0]['notification_id']
        else:
            return 0

    def run (self):
        last_nid = self.get_remote_last_nid ()
        if last_nid == self.last_nid:
            logging.debug ("no need to update notification")
            return
        self.get_remote_current_status ()
        time.sleep (1)
        self.get_remote_notification ()
        time.sleep (1)
        self.get_remote_comments ()
        time.sleep (1)
        self.get_remote_users_icon ()
        time.sleep (1)
        self.get_remote_applications_icon ()
        time.sleep (1)
        self.updated_timestamp = datetime.date.today ()
        logging.debug ("updated finish")

def main ():
    dbus.mainloop.glib.DBusGMainLoop (set_as_default=True)

    bus = dbus.SessionBus ()
    name = dbus.service.BusName ("org.wallbox.PostmanService", bus)
    obj = Postman (bus, "/org/wallbox/PostmanObject")

    mainloop = gobject.MainLoop ()
    mainloop.run ()

if __name__ == "__main__":
    main ()
