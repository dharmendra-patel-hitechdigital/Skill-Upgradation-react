import Card from '../ui/Card.jsx'

const TYPE_STYLES = {
  order: { icon: '▤', tone: 'blue' },
  upgrade: { icon: '▲', tone: 'green' },
  refund: { icon: '↩', tone: 'red' },
  signup: { icon: '＋', tone: 'violet' },
}

export default function RecentActivity({ activity = [] }) {
  return (
    <Card className="activity-card">
      <div className="activity-card__head">
        <h3 className="activity-card__title">Recent Activity</h3>
        <a href="#all" className="activity-card__link">View all</a>
      </div>

      <ul className="activity-list">
        {activity.map((item) => {
          const style = TYPE_STYLES[item.type] ?? TYPE_STYLES.order
          return (
            <li className="activity-item" key={item.id}>
              <span className={`activity-item__icon activity-item__icon--${style.tone}`}>
                {style.icon}
              </span>
              <div className="activity-item__body">
                <p className="activity-item__text">
                  <strong>{item.user}</strong> {item.action}
                </p>
                <span className="activity-item__time">{item.time}</span>
              </div>
              {item.amount && <span className="activity-item__amount">{item.amount}</span>}
            </li>
          )
        })}
      </ul>
    </Card>
  )
}
