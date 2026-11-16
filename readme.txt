青年人才住房补贴与租金减免清算中心
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q housing_relief tests
验收：python3 -m housing_relief --database /tmp/housing_relief.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/housing_relief-output.json
单测：python3 -m unittest tests.test_domain -v
