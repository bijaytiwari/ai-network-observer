SWITCHES = [f's{i}' for i in range(1, 9)]
HOSTS = {'h1': 's5', 'h2': 's5', 'h3': 's5', 'h4': 's6', 'h5': 's6',
         'h6': 's7', 'h7': 's7', 'h8': 's8', 'h9': 's8', 'h10': 's8'}   # host: access switch
SW_LINKS = [('s1', 's2'), ('s3', 's1'), ('s3', 's2'), ('s4', 's1'), ('s4', 's2'),
            ('s5', 's3'), ('s6', 's3'), ('s7', 's4'), ('s8', 's4')]
