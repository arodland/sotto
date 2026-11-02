/* bench-only stand-ins for the C library headers (cm4/shim.c) */
#pragma once
typedef struct FILE FILE;
extern FILE *stderr;
int fprintf(FILE *f, const char *fmt, ...);
