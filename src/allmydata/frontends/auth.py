import os

from zope.interface import implementer
from twisted.web.client import getPage
from twisted.internet import defer
from twisted.cred import error, checkers, credentials
from twisted.conch.ssh import keys
from twisted.conch.checkers import SSHPublicKeyChecker, InMemorySSHKeyDB

from allmydata.util.dictutil import BytesKeyDict
from allmydata.util import base32
from allmydata.util.fileutil import abspath_expanduser_unicode


class NeedRootcapLookupScheme(Exception):
    """Accountname+Password-based access schemes require some kind of
    mechanism to translate name+passwd pairs into a rootcap, either a file of
    name/passwd/rootcap tuples, or a server to do the translation."""

class FTPAvatarID(object):
    def __init__(self, username, rootcap):
        self.username = username
        self.rootcap = rootcap

@implementer(checkers.ICredentialsChecker)
class AccountFileChecker(object):
    credentialInterfaces = (credentials.IUsernamePassword,
                            credentials.IUsernameHashedPassword,
                            credentials.ISSHPrivateKey)
    def __init__(self, client, accountfile):
        self.client = client
        self.passwords = BytesKeyDict()
        pubkeys = BytesKeyDict()
        self.rootcaps = BytesKeyDict()
        with open(abspath_expanduser_unicode(accountfile), "rb") as f:
            for line in f:
                line = line.strip()
                if line.startswith(b"#") or not line:
                    continue
                name, passwd, rest = line.split(None, 2)
                if passwd.startswith(b"ssh-"):
                    bits = rest.split()
                    keystring = b" ".join([passwd] + bits[:-1])
                    key = keys.Key.fromString(keystring)
                    rootcap = bits[-1]
                    pubkeys[name] = [key]
                else:
                    self.passwords[name] = passwd
                    rootcap = rest
                self.rootcaps[name] = rootcap
        self._pubkeychecker = SSHPublicKeyChecker(InMemorySSHKeyDB(pubkeys))

    def _avatarId(self, username):
        return FTPAvatarID(username, self.rootcaps[username])

    def _cbPasswordMatch(self, matched, username):
        if matched:
            return self._avatarId(username)
        raise error.UnauthorizedLogin

    def requestAvatarId(self, creds):
        if credentials.ISSHPrivateKey.providedBy(creds):
            d = defer.maybeDeferred(self._pubkeychecker.requestAvatarId, creds)
            d.addCallback(self._avatarId)
            return d
        elif credentials.IUsernameHashedPassword.providedBy(creds):
            return self._checkPassword(creds)
        elif credentials.IUsernamePassword.providedBy(creds):
            return self._checkPassword(creds)
        else:
            raise NotImplementedError()

    def _checkPassword(self, creds):
        """
        Determine whether the password in the given credentials matches the
        password in the account file.

        Returns a Deferred that fires with the username if the password matches
        or with an UnauthorizedLogin failure otherwise.
        """
        try:
            correct = self.passwords[creds.username]
        except KeyError:
            return defer.fail(error.UnauthorizedLogin())

        d = defer.maybeDeferred(creds.checkPassword, correct)
        d.addCallback(self._cbPasswordMatch, str(creds.username))
        return d


@implementer(checkers.ICredentialsChecker)
class AccountURLChecker(object):
    credentialInterfaces = (credentials.IUsernamePassword,)

    def __init__(self, client, auth_url):
        self.client = client
        self.auth_url = auth_url

    def _cbPasswordMatch(self, rootcap, username):
        return FTPAvatarID(username, rootcap)

    def post_form(self, username, password):
        sepbase = base32.b2a(os.urandom(4))
        sep = "--" + sepbase
        form = []
        form.append(sep)
        fields = {"action": "authenticate",
                  "email": username,
                  "passwd": password,
                  }
        for name, value in fields.iteritems():
            form.append('Content-Disposition: form-data; name="%s"' % name)
            form.append('')
            assert isinstance(value, str)
            form.append(value)
            form.append(sep)
        form[-1] += "--"
        body = "\r\n".join(form) + "\r\n"
        headers = {"content-type": "multipart/form-data; boundary=%s" % sepbase,
                   }
        return getPage(self.auth_url, method="POST",
                       postdata=body, headers=headers,
                       followRedirect=True, timeout=30)

    def _parse_response(self, res):
        rootcap = res.strip()
        if rootcap == "0":
            raise error.UnauthorizedLogin
        return rootcap

    def requestAvatarId(self, credentials):
        # construct a POST to the login form. While this could theoretically
        # be done with something like the stdlib 'email' package, I can't
        # figure out how, so we just slam together a form manually.
        d = self.post_form(credentials.username, credentials.password)
        d.addCallback(self._parse_response)
        d.addCallback(self._cbPasswordMatch, str(credentials.username))
        return d

