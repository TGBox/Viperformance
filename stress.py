import multiprocessing as mp, sys, time

def burn(end):
    while time.time() < end:
        pass

if __name__ == "__main__":
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    end = time.time() + seconds
    procs = [mp.Process(target=burn, args=(end,)) for _ in range(mp.cpu_count())]
    for p in procs: p.start()
    for p in procs: p.join()