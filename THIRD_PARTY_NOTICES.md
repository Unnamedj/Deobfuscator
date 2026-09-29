# Third-party notices

## Deobfuscator-Luraph-V15

`deobf/obfuscators/luraph_v15/devirt.py`, `vmmap.py` and `driver.py` include
changes ported from https://github.com/caomod2077/Deobfuscator-Luraph-V15
(support for a Luraph VM variant that keeps its helpers in a runtime-built
table, closures wrapped in parentheses, plain-Lua closures, and repair of a
truncated dump). They were merged function by function; where a function was
taken from that project, its logic is theirs.

That project is distributed under the MIT License:

```
MIT License

Copyright (c) 2026 caomod2077

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
