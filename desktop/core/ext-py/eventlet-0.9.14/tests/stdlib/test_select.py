from eventlet import patcher
from eventlet.green import select

patcher.inject('test.test_select',
    globals(),
    ('select', select))
    
if __name__ == "__main__":
    try:
        test_main()
    except NameError:
        pass # 2.5