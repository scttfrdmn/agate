package main

import "testing"

func TestDispatchVersion(t *testing.T) {
	if got := dispatch([]string{"version"}); got != 0 {
		t.Fatalf("version exit = %d, want 0", got)
	}
}

func TestDispatchUnknownCommand(t *testing.T) {
	if got := dispatch([]string{"bogus"}); got != 2 {
		t.Fatalf("unknown command exit = %d, want 2", got)
	}
}

func TestDispatchNoArgsShowsUsage(t *testing.T) {
	if got := dispatch(nil); got != 2 {
		t.Fatalf("no-args exit = %d, want 2", got)
	}
}

func TestStubReturnsNonZero(t *testing.T) {
	if got := dispatch([]string{"deploy"}); got != 1 {
		t.Fatalf("stub exit = %d, want 1", got)
	}
}
