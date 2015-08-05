"""Snippets server.

This server runs the Khan Academy weekly snippets.  Users can
add a summary of what they did in the last week, and browse
other people's snippets.  They will also get weekly mail with
everyone's snippets in them.
"""

__author__ = 'Craig Silverstein <csilvers@khanacademy.org>'

import datetime
import logging
import os
import re
import time
import urllib

from google.appengine.api import mail
from google.appengine.api import users
from google.appengine.ext import db
import webapp2
from webapp2_extras import jinja2

import models
import util

# the URL to use for notifications directing people to this site
_URL = 'http://weekly-snippets.appspot.com'


# Have the cron jobs send to HipChat in addition to email?
_SEND_TO_HIPCHAT = True
if _SEND_TO_HIPCHAT:
    import hipchatlib
    if not hipchatlib.hipchat_init():
        _SEND_TO_HIPCHAT = False
        logging.error('Unable to initialize HipChat; not sending msgs there')


# This allows mocking in a different day, for testing.
_TODAY_FN = datetime.datetime.now


jinja2.default_config['template_path'] = os.path.join(
    os.path.dirname(__file__),
    "templates"
)
jinja2.default_config['filters'] = {
    'readable_date': (
        lambda value: value.strftime('%B %d, %Y').replace(' 0', ' ')),
    'iso_date': (
        lambda value: value.strftime('%m-%d-%Y')),
}


def _login_page(request, redirector):
    """Redirect the user to a page where they can log in."""
    redirector.redirect(users.create_login_url(request.uri))


def _current_user_email():
    """Return the logged-in user's email address, converted into lowercase."""
    return users.get_current_user().email().lower()


def _get_or_create_user(email):
    """Return the user object with the given email, creating if if needed.

    Considers the permissions scope of the currently logged in web user,
    and raises an IndexError if the currently logged in user is not the same as
    the queried email address (or is an admin).

    NOTE: Any access that causes _get_or_create_user() is an access that
    indicates the user is active again, so they are "unhidden" in the db.
    """
    user = util.get_user(email)
    if user:
        if user.is_hidden:
            # Any access that causes _get_or_create_user() is an access
            # that indicates the user is active again, so un-hide them.
            user.is_hidden = False
            user.put()
    elif not _logged_in_user_has_permission_for(email):
        raise IndexError('User "%s" not found; did you specify'
                         ' the full email address?' % email)
    else:
        user = models.User(email=email, created=_TODAY_FN())
        db.put(user)
        db.get(user.key())    # ensure db consistency for HRD
    return user


def _logged_in_user_has_permission_for(email):
    """True if the current logged-in appengine user can edit this user."""
    return (email == _current_user_email()) or users.is_current_user_admin()


def _can_view_private_snippets(my_email, snippet_email):
    """Return true if I have permission to view other's private snippet.

    I have permission to view if I am in the same domain as the person
    who wrote the snippet (domain is everything following the @ in the
    email).

    Arguments:
      my_email: the email address of the currently logged in user
      snippet_email: the email address of the snippet we're trying to view.

    Returns:
      True if my_email has permission to view snippet_email's private
      emails, or False else.
    """
    my_at = my_email.rfind('@')
    snippet_at = snippet_email.rfind('@')
    if my_at == -1 or snippet_at == -1:
        return False    # be safe
    return my_email[my_at:] == snippet_email[snippet_at:]


def _send_to_chat(msg):
    """Sends a message to the main room/channel for active chat integrations."""
    if _SEND_TO_HIPCHAT:
        hipchatlib.send_to_hipchat_room('Khan Academy', msg)


class BaseHandler(webapp2.RequestHandler):
    """Set up as per the jinja2.py docstring."""
    @webapp2.cached_property
    def jinja2(self):
        return jinja2.get_jinja2()

    def render_response(self, template_filename, context):
        html = self.jinja2.render_template(template_filename, **context)
        self.response.write(html)


class UserPage(BaseHandler):
    """Show all the snippets for a single user."""

    def get(self):
        if not users.get_current_user():
            return _login_page(self.request, self)

        user_email = self.request.get('u', _current_user_email())
        user = util.get_user(user_email)

        if not user:
            template_values = {
                'login_url': users.create_login_url(self.request.uri),
                'logout_url': users.create_logout_url('/'),
                'username': user_email,
            }
            self.render_response('new_user.html', template_values)
            return

        snippets = util.snippets_for_user(user_email)

        if not _can_view_private_snippets(_current_user_email(), user_email):
            snippets = [snippet for snippet in snippets if not snippet.private]
        snippets = util.fill_in_missing_snippets(snippets, user,
                                                 user_email, _TODAY_FN())
        snippets.reverse()                  # get to newest snippet first

        template_values = {
            'logout_url': users.create_logout_url('/'),
            'message': self.request.get('msg'),
            'username': user_email,
            'is_admin': users.is_current_user_admin(),
            'domain': user_email.split('@')[-1],
            'view_week': util.existingsnippet_monday(_TODAY_FN()),
            # Snippets for the week of <one week ago> are due today.
            'one_week_ago': _TODAY_FN().date() - datetime.timedelta(days=7),
            'eight_days_ago': _TODAY_FN().date() - datetime.timedelta(days=8),
            'editable': (_logged_in_user_has_permission_for(user_email) and
                         self.request.get('edit', '1') == '1'),
            'user': user,
            'snippets': snippets,
            'null_snippet_text': models.NULL_SNIPPET_TEXT,
            'null_category': models.NULL_CATEGORY,
        }
        self.render_response('user_snippets.html', template_values)


def _title_case(s):
    """Like string.title(), but does not uppercase 'and'."""
    # Smarter would be to use 'pip install titlecase'.
    SMALL = 'a|an|and|as|at|but|by|en|for|if|in|of|on|or|the|to|v\.?|via|vs\.?'
    # We purposefully don't match small words at the beginning of a string.
    SMALL_RE = re.compile(r' (%s)\b' % SMALL, re.I)
    return SMALL_RE.sub(lambda m: ' ' + m.group(1).lower(), s.title())


class SummaryPage(BaseHandler):
    """Show all the snippets for a single week."""

    def get(self):
        if not users.get_current_user():
            return _login_page(self.request, self)

        week_string = self.request.get('week')
        if week_string:
            week = datetime.datetime.strptime(week_string, '%m-%d-%Y').date()
        else:
            week = util.existingsnippet_monday(_TODAY_FN())

        snippets_q = models.Snippet.all()
        snippets_q.filter('week = ', week)
        snippets = snippets_q.fetch(1000)   # good for many users...
        # TODO(csilvers): filter based on wants_to_view

        # Get all the user records so we can categorize snippets.
        user_q = models.User.all()
        results = user_q.fetch(1000)
        email_to_category = {}
        hidden_users = set()      # emails of users with the 'hidden' property
        for result in results:
            # People aren't very good about capitalizing their
            # categories consistently, so we enforce title-case,
            # with exceptions for 'and'.
            email_to_category[result.email] = _title_case(result.category)
            if result.is_hidden:
                hidden_users.add(result.email)

        # Collect the snippets by category.  As we see each email,
        # delete it from email_to_category.  At the end of this,
        # email_to_category will hold people who did not give
        # snippets this week.
        snippets_by_category = {}
        for snippet in snippets:
            # Ignore this snippet if we don't have permission to view it.
            if (snippet.private and
                    not _can_view_private_snippets(_current_user_email(),
                                                   snippet.email)):
                continue
            category = email_to_category.get(
                snippet.email, models.NULL_CATEGORY
            )
            snippets_by_category.setdefault(category, []).append(snippet)
            if snippet.email in email_to_category:
                del email_to_category[snippet.email]

        # Add in empty snippets for the people who didn't have any --
        # unless a user is marked 'hidden'.  (That's what 'hidden'
        # means: pretend they don't exist until they have a non-empty
        # snippet again.)
        for (email, category) in email_to_category.iteritems():
            if email not in hidden_users:
                snippet = models.Snippet(email=email, week=week,
                                         text='(no snippet this week)')
                snippets_by_category.setdefault(category, []).append(snippet)

        # Now get a sorted list, categories in alphabetical order and
        # each snippet-author within the category in alphabetical
        # order.  The data structure is ((category, (snippet, ...)), ...)
        categories_and_snippets = []
        for (category, snippets) in snippets_by_category.iteritems():
            snippets.sort(key=lambda snippet: snippet.email)
            categories_and_snippets.append((category, snippets))
        categories_and_snippets.sort()

        template_values = {
            'logout_url': users.create_logout_url('/'),
            'message': self.request.get('msg'),
            # Used only to switch to 'username' mode and to modify settings.
            'username': _current_user_email(),
            'is_admin': users.is_current_user_admin(),
            'prev_week': week - datetime.timedelta(7),
            'view_week': week,
            'next_week': week + datetime.timedelta(7),
            'categories_and_snippets': categories_and_snippets,
        }
        self.render_response('weekly_snippets.html', template_values)


class UpdateSnippet(BaseHandler):
    def update_snippet(self, email):
        week_string = self.request.get('week')
        week = datetime.datetime.strptime(week_string, '%m-%d-%Y').date()
        assert week.weekday() == 0, 'passed-in date must be a Monday'

        text = self.request.get('snippet')

        private = self.request.get('private') == 'True'
        is_markdown = self.request.get('is_markdown') == 'True'

        q = models.Snippet.all()
        q.filter('email = ', email)
        q.filter('week = ', week)
        snippet = q.get()

        if snippet:
            snippet.text = text   # just update the snippet text
            snippet.private = private
            snippet.is_markdown = is_markdown
        else:
            # add the snippet to the db
            snippet = models.Snippet(created=_TODAY_FN(), email=email,
                                     week=week, text=text, private=private,
                                     is_markdown=is_markdown)
        db.put(snippet)
        db.get(snippet.key())  # ensure db consistency for HRD

        # When adding a snippet, make sure we create a user record for
        # that email as well, if it doesn't already exist.
        _get_or_create_user(email)
        self.response.set_status(200)

    def post(self):
        """handle ajax updates via POST

        in particular, return status via json rather than redirects and
        hard exceptions. This isn't actually RESTy, it's just status
        codes and json.
        """
        # TODO(marcos): consider using PUT?

        self.response.headers['Content-Type'] = 'application/json'

        if not users.get_current_user():
            # 403s are the catch-all 'please log in error' here
            self.response.set_status(403)
            self.response.out.write('{"status": 403, '
                                    '"message": "not logged in"}')
            return

        email = self.request.get('u', _current_user_email())

        if not _logged_in_user_has_permission_for(email):
            # TODO(marcos): present these messages to the ajax client
            self.response.set_status(403)
            error = 'You do not have permissions to update user' \
                ' snippets for %s' % email
            self.response.out.write('{"status": 403, '
                                    '"message": "%s"}' % error)
            return

        self.update_snippet(email)
        self.response.out.write('{"status": 200, "message": "ok"}')

    def get(self):
        if not users.get_current_user():
            return _login_page(self.request, self)

        email = self.request.get('u', _current_user_email())
        if not _logged_in_user_has_permission_for(email):
            # TODO(csilvers): return a 403 here instead.
            raise RuntimeError('You do not have permissions to update user'
                               ' snippets for %s' % email)

        self.update_snippet(email)

        email = self.request.get('u', _current_user_email())
        self.redirect("/?msg=Snippet+saved&u=%s" % urllib.quote(email))


class Settings(BaseHandler):
    """Page to display a user's settings (from class User) for modification."""

    def get(self):
        if not users.get_current_user():
            return _login_page(self.request, self)

        user_email = self.request.get('u', _current_user_email())
        if not _logged_in_user_has_permission_for(user_email):
            # TODO(csilvers): return a 403 here instead.
            raise RuntimeError('You do not have permissions to view user'
                               ' settings for %s' % user_email)
        user = _get_or_create_user(user_email)

        template_values = {
            'logout_url': users.create_logout_url('/'),
            'message': self.request.get('msg'),
            'username': user.email,
            'is_admin': users.is_current_user_admin(),
            'view_week': util.existingsnippet_monday(_TODAY_FN()),
            'user': user,
            'redirect_to': self.request.get('redirect_to', ''),
            # We could get this from user, but we want to replace
            # commas with newlines for printing.
            'wants_to_view': user.wants_to_view.replace(',', '\n'),
        }
        self.render_response('settings.html', template_values)


class UpdateSettings(BaseHandler):
    """Updates the db with modifications from the Settings page."""

    def get(self):
        if not users.get_current_user():
            return _login_page(self.request, self)

        user_email = self.request.get('u', _current_user_email())
        if not _logged_in_user_has_permission_for(user_email):
            # TODO(csilvers): return a 403 here instead.
            raise RuntimeError('You do not have permissions to modify user'
                               ' settings for %s' % user_email)
        user = _get_or_create_user(user_email)

        category = self.request.get('category')
        uses_markdown = self.request.get('markdown') == 'yes'
        private_snippets = self.request.get('private') == 'yes'
        wants_email = self.request.get('reminder_email') == 'yes'

        # We want this list to be comma-separated, but people are
        # likely to use both commas and newlines to separate.  Convert
        # here.  Also get rid of whitespace, which cannot be in emails.
        wants_to_view = self.request.get('to_view').replace('\n', ',')
        wants_to_view = wants_to_view.replace(' ', '')

        # Changing their settings is the kind of activity that unhides
        # someone who was hidden, unless they specifically ask to be
        # hidden.
        is_hidden = self.request.get('is_hidden', 'no') == 'yes'

        user.is_hidden = is_hidden
        user.category = category or models.NULL_CATEGORY
        user.uses_markdown = uses_markdown
        user.private_snippets = private_snippets
        user.wants_email = wants_email
        user.wants_to_view = wants_to_view
        db.put(user)
        db.get(user.key())  # ensure db consistency for HRD

        redirect_to = self.request.get('redirect_to')
        if redirect_to == 'snippet_entry':   # true for new_user.html
            self.redirect('/?u=%s' % urllib.quote(user_email))
        else:
            self.redirect("/settings?msg=Changes+saved&u=%s"
                          % urllib.quote(user_email))


class ManageUsers(BaseHandler):
    """Lets admins delete and otherwise manage users."""

    def get(self):
        # options are 'email', 'creation_time', 'last_snippet_time'
        sort_by = self.request.get('sort_by', 'creation_time')

        # First, check if the user had clicked on a button.
        for (name, value) in self.request.params.iteritems():
            if name.startswith('hide '):
                email_of_user_to_hide = name[len('hide '):]
                user = util.get_user_or_die(email_of_user_to_hide)
                user.is_hidden = True
                user.put()
                time.sleep(0.1)   # encourage eventual consistency
                self.redirect('/admin/manage_users?sort_by=%s&msg=%s+hidden'
                              % (sort_by, email_of_user_to_hide))
                return
            if name.startswith('unhide '):
                email_of_user_to_unhide = name[len('unhide '):]
                user = util.get_user_or_die(email_of_user_to_unhide)
                user.is_hidden = False
                user.put()
                time.sleep(0.1)   # encourage eventual consistency
                self.redirect('/admin/manage_users?sort_by=%s&msg=%s+unhidden'
                              % (sort_by, email_of_user_to_unhide))
                return
            if name.startswith('delete '):
                email_of_user_to_delete = name[len('delete '):]
                user = util.get_user_or_die(email_of_user_to_delete)
                db.delete(user)
                time.sleep(0.1)   # encourage eventual consistency
                self.redirect('/admin/manage_users?sort_by=%s&msg=%s+deleted'
                              % (sort_by, email_of_user_to_delete))
                return

        user_q = models.User.all()
        results = user_q.fetch(1000)

        # Tuple: (email, is-hidden, creation-time, days since last snippet)
        user_data = []
        for user in results:
            # Get the last snippet for that user.
            snippets = util.snippets_for_user(user.email)
            if snippets:
                seconds_since_snippet = (
                    (_TODAY_FN().date() - snippets[-1].week).total_seconds())
                weeks_since_snippet = int(
                    seconds_since_snippet /
                    datetime.timedelta(days=7).total_seconds())
            else:
                weeks_since_snippet = None
            user_data.append((user.email, user.is_hidden,
                              user.created, weeks_since_snippet))

        # We have to use 'cmp' here since we want ascending in the
        # primary key and descending in the secondary key, sometimes.
        if sort_by == 'email':
            user_data.sort(lambda x, y: cmp(x[0], y[0]))
        elif sort_by == 'creation_time':
            user_data.sort(lambda x, y: (-cmp(x[2] or datetime.datetime.min,
                                              y[2] or datetime.datetime.min)
                                         or cmp(x[0], y[0])))
        elif sort_by == 'last_snippet_time':
            user_data.sort(lambda x, y: (-cmp(1000 if x[3] is None else x[3],
                                              1000 if y[3] is None else y[3])
                                         or cmp(x[0], y[0])))
        else:
            raise ValueError('Invalid sort_by value "%s"' % sort_by)

        template_values = {
            'logout_url': users.create_logout_url('/'),
            'message': self.request.get('msg'),
            'username': _current_user_email(),
            'is_admin': users.is_current_user_admin(),
            'view_week': util.existingsnippet_monday(_TODAY_FN()),
            'user_data': user_data,
            'sort_by': sort_by,
        }
        self.render_response('manage_users.html', template_values)


# The following two classes are called by cron.


def _get_email_to_current_snippet_map(today):
    """Return a map from email to True if they've written snippets this week.

    Goes through all users registered on the system, and checks if
    they have a snippet in the db for the appropriate snippet-week for
    'today'.  If so, they get entered into the return-map with value
    True.  If not, they have value False.

    Note that users whose 'wants_email' field is set to False will not
    be included in either list.

    Arguments:
      today: a datetime.datetime object representing the
        'current' day.  We use the normal algorithm to determine what is
        the most recent snippet-week for this day.

    Returns:
      a map from email (user.email for each user) to True or False,
      depending on if they've written snippets for this week or not.
    """
    user_q = models.User.all()
    users = user_q.fetch(1000)
    retval = {}
    for user in users:
        if not user.wants_email:         # ignore this user
            continue
        retval[user.email] = False       # assume the worst, for now

    week = util.existingsnippet_monday(today)
    snippets_q = models.Snippet.all()
    snippets_q.filter('week = ', week)
    snippets = snippets_q.fetch(1000)
    for snippet in snippets:
        if snippet.email in retval:      # don't introduce new keys here
            retval[snippet.email] = True

    return retval


def _send_snippets_mail(to, subject, template_path, template_values):
    jinja2_instance = jinja2.get_jinja2()
    mail.send_mail(sender=('Khan Academy Snippet Server'
                           ' <csilvers+snippets@khanacademy.org>'),
                   to=to,
                   subject=subject,
                   body=jinja2_instance.render_template(template_path,
                                                        **template_values))
    # Appengine has a quota of 32 emails per minute:
    #    https://developers.google.com/appengine/docs/quotas#Mail
    # We pause 2 seconds between each email to make sure we
    # don't go over that.
    time.sleep(2)


class SendFridayReminderHipChat(BaseHandler):
    """Send a chat message to the main KA room."""

    def get(self):
        msg = 'Reminder: Weekly snippets due Monday at 5pm. ' + _URL
        _send_to_chat(msg)


class SendReminderEmail(BaseHandler):
    """Send an email to everyone who doesn't have a snippet for this week."""

    def _send_mail(self, email):
        template_values = {}
        _send_snippets_mail(email, 'Weekly snippets due today at 5pm',
                            'reminder_email.txt', template_values)

    def get(self):
        email_to_has_snippet = _get_email_to_current_snippet_map(_TODAY_FN())
        for (user_email, has_snippet) in email_to_has_snippet.iteritems():
            if not has_snippet:
                self._send_mail(user_email)
                logging.debug('sent reminder email to %s' % user_email)
            else:
                logging.debug('did not send reminder email to %s: '
                              'has a snippet already' % user_email)

        msg = 'Reminder: Weekly snippets due today at 5pm. ' + _URL
        _send_to_chat(msg)


class SendViewEmail(BaseHandler):
    """Send an email to everyone to look at the week's snippets."""

    def _send_mail(self, email, has_snippets):
        template_values = {'has_snippets': has_snippets}
        _send_snippets_mail(email, 'Weekly snippets are ready!',
                            'view_email.txt', template_values)

    def get(self):
        email_to_has_snippet = _get_email_to_current_snippet_map(_TODAY_FN())
        for (user_email, has_snippet) in email_to_has_snippet.iteritems():
            self._send_mail(user_email, has_snippet)
            logging.debug('sent "view" email to %s' % user_email)

        msg = 'Weekly snippets are ready! ' + _URL + '/weekly'
        _send_to_chat(msg)


application = webapp2.WSGIApplication([
    ('/', UserPage),
    ('/weekly', SummaryPage),
    ('/update_snippet', UpdateSnippet),
    ('/settings', Settings),
    ('/update_settings', UpdateSettings),
    ('/admin/manage_users', ManageUsers),
    ('/admin/send_friday_reminder_hipchat', SendFridayReminderHipChat),
    ('/admin/send_reminder_email', SendReminderEmail),
    ('/admin/send_view_email', SendViewEmail),
    ('/admin/test_send_to_hipchat', hipchatlib.TestSendToHipchat),
    ],
    debug=True)
