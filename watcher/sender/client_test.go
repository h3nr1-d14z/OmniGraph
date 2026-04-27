package sender

import (
	"net/http"
	"reflect"
	"testing"
	"time"
)

// NewClient must clone http.DefaultTransport so HTTP_PROXY / HTTPS_PROXY /
// NO_PROXY environment variables are still honored after our idle-knob
// override. A struct-literal Transport would silently drop ProxyFromEnvironment.
func TestNewClientPreservesProxyFromEnvironment(t *testing.T) {
	c := NewClient("http://hub:9000", "tok", "m1")
	tr, ok := c.HTTP.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("Transport is %T, want *http.Transport", c.HTTP.Transport)
	}
	if tr.Proxy == nil {
		t.Fatalf("Transport.Proxy is nil — DefaultTransport.Proxy was lost")
	}
	// ProxyFromEnvironment is a function value; identity comparison via
	// reflect.ValueOf().Pointer() is the standard way to confirm we still
	// have THE Default proxy resolver and not a hand-rolled one.
	want := reflect.ValueOf(http.ProxyFromEnvironment).Pointer()
	got := reflect.ValueOf(tr.Proxy).Pointer()
	if got != want {
		t.Errorf("Transport.Proxy not http.ProxyFromEnvironment (pointer mismatch)")
	}
}

func TestNewClientAppliesIdleConnectionKnobs(t *testing.T) {
	c := NewClient("http://hub:9000", "tok", "m1")
	tr := c.HTTP.Transport.(*http.Transport)
	if tr.MaxIdleConns != 100 {
		t.Errorf("MaxIdleConns = %d, want 100", tr.MaxIdleConns)
	}
	if tr.MaxIdleConnsPerHost != 10 {
		t.Errorf("MaxIdleConnsPerHost = %d, want 10", tr.MaxIdleConnsPerHost)
	}
	if tr.IdleConnTimeout != 90*time.Second {
		t.Errorf("IdleConnTimeout = %v, want 90s", tr.IdleConnTimeout)
	}
}
