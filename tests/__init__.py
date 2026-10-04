import os

# The mock servers listen on localhost; never send that through an outbound proxy.
for _name in ("NO_PROXY", "no_proxy"):
    os.environ[_name] = "127.0.0.1,localhost," + os.environ.get(_name, "")
